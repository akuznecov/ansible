# (c) 2012, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import sys
import re
import os
import shlex
import yaml
import copy
import optparse
import operator
from ansible import errors
from ansible import __version__
from ansible.utils.plugins import *
from ansible.utils import template
from ansible.callbacks import display
import ansible.constants as C
import time
import StringIO
import stat
import termios
import tty
import pipes
import random
import difflib
import warnings
import traceback
import getpass
import sys
import textwrap

VERBOSITY=0

# list of all deprecation messages to prevent duplicate display
deprecations = {}
warns = {}

MAX_FILE_SIZE_FOR_DIFF=1*1024*1024

try:
    import json
except ImportError:
    import simplejson as json

try:
    from hashlib import md5 as _md5
except ImportError:
    from md5 import md5 as _md5

PASSLIB_AVAILABLE = False
try:
    import passlib.hash
    PASSLIB_AVAILABLE = True
except:
    pass

KEYCZAR_AVAILABLE=False
try:
    import keyczar.errors as key_errors
    from keyczar.keys import AesKey
    KEYCZAR_AVAILABLE=True
except ImportError:
    pass

###############################################################
# Abstractions around keyczar
###############################################################

def key_for_hostname(hostname):
    # fireball mode is an implementation of ansible firing up zeromq via SSH
    # to use no persistent daemons or key management

    if not KEYCZAR_AVAILABLE:
        raise errors.AnsibleError("python-keyczar must be installed on the control machine to use accelerated modes")

    key_path = os.path.expanduser("~/.fireball.keys")
    if not os.path.exists(key_path):
        os.makedirs(key_path)
    key_path = os.path.expanduser("~/.fireball.keys/%s" % hostname)

    # use new AES keys every 2 hours, which means fireball must not allow running for longer either
    if not os.path.exists(key_path) or (time.time() - os.path.getmtime(key_path) > 60*60*2):
        key = AesKey.Generate()
        fh = open(key_path, "w")
        fh.write(str(key))
        fh.close()
        return key
    else:
        fh = open(key_path)
        key = AesKey.Read(fh.read())
        fh.close()
        return key

def encrypt(key, msg):
    return key.Encrypt(msg)

def decrypt(key, msg):
    try:
        return key.Decrypt(msg)
    except key_errors.InvalidSignatureError:
        raise errors.AnsibleError("decryption failed")

###############################################################
# UTILITY FUNCTIONS FOR COMMAND LINE TOOLS
###############################################################

def err(msg):
    ''' print an error message to stderr '''

    print >> sys.stderr, msg

def exit(msg, rc=1):
    ''' quit with an error to stdout and a failure code '''

    err(msg)
    sys.exit(rc)

def jsonify(result, format=False):
    ''' format JSON output (uncompressed or uncompressed) '''

    if result is None:
        return "{}"
    result2 = result.copy()
    for key, value in result2.items():
        if type(value) is str:
            result2[key] = value.decode('utf-8', 'ignore')
    if format:
        return json.dumps(result2, sort_keys=True, indent=4)
    else:
        return json.dumps(result2, sort_keys=True)

def write_tree_file(tree, hostname, buf):
    ''' write something into treedir/hostname '''

    # TODO: might be nice to append playbook runs per host in a similar way
    # in which case, we'd want append mode.
    path = os.path.join(tree, hostname)
    fd = open(path, "w+")
    fd.write(buf)
    fd.close()

def is_failed(result):
    ''' is a given JSON result a failed result? '''

    return ((result.get('rc', 0) != 0) or (result.get('failed', False) in [ True, 'True', 'true']))

def is_changed(result):
    ''' is a given JSON result a changed result? '''

    return (result.get('changed', False) in [ True, 'True', 'true'])

def check_conditional(conditional, basedir, inject, fail_on_undefined=False, jinja2=False):

    if isinstance(conditional, list):
        for x in conditional:
            if not check_conditional(x, basedir, inject, fail_on_undefined=fail_on_undefined, jinja2=jinja2):
                return False
        return True

    if jinja2:
        conditional = "jinja2_compare %s" % conditional

    if conditional.startswith("jinja2_compare"):
        conditional = conditional.replace("jinja2_compare ","")
        # allow variable names
        if conditional in inject and str(inject[conditional]).find('-') == -1:
            conditional = inject[conditional]
        conditional = template.template(basedir, conditional, inject, fail_on_undefined=fail_on_undefined)
        original = str(conditional).replace("jinja2_compare ","")
        # a Jinja2 evaluation that results in something Python can eval!
        presented = "{%% if %s %%} True {%% else %%} False {%% endif %%}" % conditional
        conditional = template.template(basedir, presented, inject)
        val = conditional.strip()
        if val == presented:
            # the templating failed, meaning most likely a 
            # variable was undefined. If we happened to be 
            # looking for an undefined variable, return True,
            # otherwise fail
            if conditional.find("is undefined") != -1:
                return True
            elif conditional.find("is defined") != -1:
                return False
            else:
                raise errors.AnsibleError("error while evaluating conditional: %s" % original)
        elif val == "True":
            return True
        elif val == "False":
            return False
        else:
            raise errors.AnsibleError("unable to evaluate conditional: %s" % original)

    if not isinstance(conditional, basestring):
        return conditional

    try:
        conditional = conditional.replace("\n", "\\n")
        result = safe_eval(conditional)
        if result not in [ True, False ]:
            raise errors.AnsibleError("Conditional expression must evaluate to True or False: %s" % conditional)
        return result

    except (NameError, SyntaxError):
        raise errors.AnsibleError("Could not evaluate the expression: (%s)" % conditional)

def is_executable(path):
    '''is the given path executable?'''
    return (stat.S_IXUSR & os.stat(path)[stat.ST_MODE]
            or stat.S_IXGRP & os.stat(path)[stat.ST_MODE]
            or stat.S_IXOTH & os.stat(path)[stat.ST_MODE])

def unfrackpath(path):
    ''' 
    returns a path that is free of symlinks, environment
    variables, relative path traversals and symbols (~)
    example:
    '$HOME/../../var/mail' becomes '/var/spool/mail'
    '''
    return os.path.normpath(os.path.realpath(os.path.expandvars(os.path.expanduser(path))))

def prepare_writeable_dir(tree,mode=0777):
    ''' make sure a directory exists and is writeable '''

    # modify the mode to ensure the owner at least
    # has read/write access to this directory
    mode |= 0700

    # make sure the tree path is always expanded
    # and normalized and free of symlinks
    tree = unfrackpath(tree)

    if not os.path.exists(tree):
        try:
            os.makedirs(tree, mode)
        except (IOError, OSError), e:
            raise errors.AnsibleError("Could not make dir %s: %s" % (tree, e))
    if not os.access(tree, os.W_OK):
        raise errors.AnsibleError("Cannot write to path %s" % tree)
    return tree

def path_dwim(basedir, given):
    '''
    make relative paths work like folks expect.
    '''

    if given.startswith("/"):
        return os.path.abspath(given)
    elif given.startswith("~"):
        return os.path.abspath(os.path.expanduser(given))
    else:
        return os.path.abspath(os.path.join(basedir, given))

def path_dwim_relative(original, dirname, source, playbook_base, check=True):
    ''' find one file in a directory one level up in a dir named dirname relative to current '''
    # (used by roles code)

    basedir = os.path.dirname(original)
    if os.path.islink(basedir):
        basedir = unfrackpath(basedir)
        template2 = os.path.join(basedir, dirname, source)
    else:
        template2 = os.path.join(basedir, '..', dirname, source)
    source2 = path_dwim(basedir, template2)
    if os.path.exists(source2):
        return source2
    obvious_local_path = path_dwim(playbook_base, source)
    if os.path.exists(obvious_local_path):
        return obvious_local_path
    if check:
        raise errors.AnsibleError("input file not found at %s or %s" % (source2, obvious_local_path))
    return source2 # which does not exist

def json_loads(data):
    ''' parse a JSON string and return a data structure '''

    return json.loads(data)

def parse_json(raw_data):
    ''' this version for module return data only '''

    orig_data = raw_data

    # ignore stuff like tcgetattr spewage or other warnings
    data = filter_leading_non_json_lines(raw_data)

    try:
        return json.loads(data)
    except:
        # not JSON, but try "Baby JSON" which allows many of our modules to not
        # require JSON and makes writing modules in bash much simpler
        results = {}
        try:
            tokens = shlex.split(data)
        except:
            print "failed to parse json: "+ data
            raise

        for t in tokens:
            if t.find("=") == -1:
                raise errors.AnsibleError("failed to parse: %s" % orig_data)
            (key,value) = t.split("=", 1)
            if key == 'changed' or 'failed':
                if value.lower() in [ 'true', '1' ]:
                    value = True
                elif value.lower() in [ 'false', '0' ]:
                    value = False
            if key == 'rc':
                value = int(value)
            results[key] = value
        if len(results.keys()) == 0:
            return { "failed" : True, "parsed" : False, "msg" : orig_data }
        return results

def smush_braces(data):
    ''' smush Jinaj2 braces so unresolved templates like {{ foo }} don't get parsed weird by key=value code '''
    while data.find('{{ ') != -1:
        data = data.replace('{{ ', '{{')
    while data.find(' }}') != -1:
        data = data.replace(' }}', '}}')
    return data

def smush_ds(data):
    # things like key={{ foo }} are not handled by shlex.split well, so preprocess any YAML we load
    # so we do not have to call smush elsewhere
    if type(data) == list:
        return [ smush_ds(x) for x in data ]
    elif type(data) == dict:
        for (k,v) in data.items():
            data[k] = smush_ds(v)
        return data
    elif isinstance(data, basestring):
        return smush_braces(data)
    else:
        return data

def parse_yaml(data):
    ''' convert a yaml string to a data structure '''
    return smush_ds(yaml.safe_load(data))

def process_common_errors(msg, probline, column):
    replaced = probline.replace(" ","")

    if replaced.find(":{{") != -1 and replaced.find("}}") != -1:
        msg = msg + """
This one looks easy to fix.  YAML thought it was looking for the start of a 
hash/dictionary and was confused to see a second "{".  Most likely this was
meant to be an ansible template evaluation instead, so we have to give the 
parser a small hint that we wanted a string instead. The solution here is to 
just quote the entire value.

For instance, if the original line was:

    app_path: {{ base_path }}/foo

It should be written as:

    app_path: "{{ base_path }}/foo"
"""
        return msg

    elif len(probline) and len(probline) >= column and probline[column] == ":" and probline.count(':') > 1:
        msg = msg + """
This one looks easy to fix.  There seems to be an extra unquoted colon in the line 
and this is confusing the parser. It was only expecting to find one free 
colon. The solution is just add some quotes around the colon, or quote the 
entire line after the first colon.

For instance, if the original line was:

    copy: src=file.txt dest=/path/filename:with_colon.txt

It can be written as:

    copy: src=file.txt dest='/path/filename:with_colon.txt'

Or:
    
    copy: 'src=file.txt dest=/path/filename:with_colon.txt'


"""
        return msg
    else:
        parts = probline.split(":")
        if len(parts) > 1:
            middle = parts[1].strip()
            match = False
            unbalanced = False
            if middle.startswith("'") and not middle.endswith("'"):
                match = True
            elif middle.startswith('"') and not middle.endswith('"'):
                match = True
            if middle[0] in [ '"', "'" ] and middle[-1] in [ '"', "'" ] and probline.count("'") > 2 or probline.count("'") > 2:
                unbalanced = True
            if match:
                msg = msg + """
This one looks easy to fix.  It seems that there is a value started 
with a quote, and the YAML parser is expecting to see the line ended 
with the same kind of quote.  For instance:

    when: "ok" in result.stdout

Could be written as:

   when: '"ok" in result.stdout'

or equivalently:

   when: "'ok' in result.stdout"

"""
                return msg

            if unbalanced:
                msg = msg + """
We could be wrong, but this one looks like it might be an issue with 
unbalanced quotes.  If starting a value with a quote, make sure the 
line ends with the same set of quotes.  For instance this arbitrary 
example:

    foo: "bad" "wolf"

Could be written as:

    foo: '"bad" "wolf"'

"""
                return msg

    return msg

def process_yaml_error(exc, data, path=None):
    if hasattr(exc, 'problem_mark'):
        mark = exc.problem_mark
        if mark.line -1 >= 0:
            before_probline = data.split("\n")[mark.line-1]
        else:
            before_probline = ''
        probline = data.split("\n")[mark.line]
        arrow = " " * mark.column + "^"
        msg = """Syntax Error while loading YAML script, %s
Note: The error may actually appear before this position: line %s, column %s

%s
%s
%s""" % (path, mark.line + 1, mark.column + 1, before_probline, probline, arrow)

        msg = process_common_errors(msg, probline, mark.column)


    else:
        # No problem markers means we have to throw a generic
        # "stuff messed up" type message. Sry bud.
        if path:
            msg = "Could not parse YAML. Check over %s again." % path
        else:
            msg = "Could not parse YAML."
    raise errors.AnsibleYAMLValidationFailed(msg)


def parse_yaml_from_file(path):
    ''' convert a yaml file to a data structure '''

    try:
        data = file(path).read()
        return parse_yaml(data)
    except IOError:
        raise errors.AnsibleError("file not found: %s" % path)
    except yaml.YAMLError, exc:
        process_yaml_error(exc, data, path)

def parse_kv(args):
    ''' convert a string of key/value items to a dict '''
    options = {}
    if args is not None:
        # attempting to split a unicode here does bad things
        args = args.encode('utf-8')
        vargs = [x.decode('utf-8') for x in shlex.split(args, posix=True)]
        #vargs = shlex.split(str(args), posix=True)
        for x in vargs:
            if x.find("=") != -1:
                k, v = x.split("=",1)
                options[k]=v
    return options

def merge_hash(a, b):
    ''' recursively merges hash b into a
    keys from b take precedence over keys from a '''

    result = copy.deepcopy(a)

    # next, iterate over b keys and values
    for k, v in b.iteritems():
        # if there's already such key in a
        # and that key contains dict
        if k in result and isinstance(result[k], dict):
            # merge those dicts recursively
            result[k] = merge_hash(a[k], v)
        else:
            # otherwise, just copy a value from b to a
            result[k] = v

    return result

def md5s(data):
    ''' Return MD5 hex digest of data. '''

    digest = _md5()
    try:
        digest.update(data)
    except UnicodeEncodeError:
        digest.update(data.encode('utf-8'))
    return digest.hexdigest()

def md5(filename):
    ''' Return MD5 hex digest of local file, or None if file is not present. '''

    if not os.path.exists(filename):
        return None
    digest = _md5()
    blocksize = 64 * 1024
    infile = open(filename, 'rb')
    block = infile.read(blocksize)
    while block:
        digest.update(block)
        block = infile.read(blocksize)
    infile.close()
    return digest.hexdigest()

def default(value, function):
    ''' syntactic sugar around lazy evaluation of defaults '''
    if value is None:
        return function()
    return value

def _gitinfo():
    ''' returns a string containing git branch, commit id and commit date '''
    result = None
    repo_path = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.git')

    if os.path.exists(repo_path):
        # Check if the .git is a file. If it is a file, it means that we are in a submodule structure.
        if os.path.isfile(repo_path):
            try:
                gitdir = yaml.safe_load(open(repo_path)).get('gitdir')
                # There is a posibility the .git file to have an absolute path.
                if os.path.isabs(gitdir):
                    repo_path = gitdir
                else:
                    repo_path = os.path.join(repo_path.split('.git')[0], gitdir)
            except (IOError, AttributeError):
                return ''
        f = open(os.path.join(repo_path, "HEAD"))
        branch = f.readline().split('/')[-1].rstrip("\n")
        f.close()
        branch_path = os.path.join(repo_path, "refs", "heads", branch)
        if os.path.exists(branch_path):
            f = open(branch_path)
            commit = f.readline()[:10]
            f.close()
            date = time.localtime(os.stat(branch_path).st_mtime)
            if time.daylight == 0:
                offset = time.timezone
            else:
                offset = time.altzone
            result = "({0} {1}) last updated {2} (GMT {3:+04d})".format(branch, commit,
                time.strftime("%Y/%m/%d %H:%M:%S", date), offset / -36)
    else:
        result = ''
    return result

def version(prog):
    result = "{0} {1}".format(prog, __version__)
    gitinfo = _gitinfo()
    if gitinfo:
        result = result + " {0}".format(gitinfo)
    return result

def getch():
    ''' read in a single character '''
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

####################################################################
# option handling code for /usr/bin/ansible and ansible-playbook
# below this line

class SortedOptParser(optparse.OptionParser):
    '''Optparser which sorts the options by opt before outputting --help'''

    def format_help(self, formatter=None):
        self.option_list.sort(key=operator.methodcaller('get_opt_string'))
        return optparse.OptionParser.format_help(self, formatter=None)

def increment_debug(option, opt, value, parser):
    global VERBOSITY
    VERBOSITY += 1

def base_parser(constants=C, usage="", output_opts=False, runas_opts=False,
    async_opts=False, connect_opts=False, subset_opts=False, check_opts=False, diff_opts=False):
    ''' create an options parser for any ansible script '''

    parser = SortedOptParser(usage, version=version("%prog"))
    parser.add_option('-v','--verbose', default=False, action="callback",
        callback=increment_debug, help="verbose mode (-vvv for more, -vvvv to enable connection debugging)")

    parser.add_option('-f','--forks', dest='forks', default=constants.DEFAULT_FORKS, type='int',
        help="specify number of parallel processes to use (default=%s)" % constants.DEFAULT_FORKS)
    parser.add_option('-i', '--inventory-file', dest='inventory',
        help="specify inventory host file (default=%s)" % constants.DEFAULT_HOST_LIST,
        default=constants.DEFAULT_HOST_LIST)
    parser.add_option('-k', '--ask-pass', default=False, dest='ask_pass', action='store_true',
        help='ask for SSH password')
    parser.add_option('--private-key', default=C.DEFAULT_PRIVATE_KEY_FILE, dest='private_key_file',
        help='use this file to authenticate the connection')
    parser.add_option('-K', '--ask-sudo-pass', default=False, dest='ask_sudo_pass', action='store_true',
        help='ask for sudo password')
    parser.add_option('--list-hosts', dest='listhosts', action='store_true',
        help='outputs a list of matching hosts; does not execute anything else')
    parser.add_option('-M', '--module-path', dest='module_path',
        help="specify path(s) to module library (default=%s)" % constants.DEFAULT_MODULE_PATH,
        default=None)

    if subset_opts:
        parser.add_option('-l', '--limit', default=constants.DEFAULT_SUBSET, dest='subset',
            help='further limit selected hosts to an additional pattern')

    parser.add_option('-T', '--timeout', default=constants.DEFAULT_TIMEOUT, type='int',
        dest='timeout',
        help="override the SSH timeout in seconds (default=%s)" % constants.DEFAULT_TIMEOUT)

    if output_opts:
        parser.add_option('-o', '--one-line', dest='one_line', action='store_true',
            help='condense output')
        parser.add_option('-t', '--tree', dest='tree', default=None,
            help='log output to this directory')

    if runas_opts:
        parser.add_option("-s", "--sudo", default=constants.DEFAULT_SUDO, action="store_true",
            dest='sudo', help="run operations with sudo (nopasswd)")
        parser.add_option('-U', '--sudo-user', dest='sudo_user', help='desired sudo user (default=root)',
            default=None)   # Can't default to root because we need to detect when this option was given
        parser.add_option('-u', '--user', default=constants.DEFAULT_REMOTE_USER,
            dest='remote_user',
            help='connect as this user (default=%s)' % constants.DEFAULT_REMOTE_USER)

    if connect_opts:
        parser.add_option('-c', '--connection', dest='connection',
                          default=C.DEFAULT_TRANSPORT,
                          help="connection type to use (default=%s)" % C.DEFAULT_TRANSPORT)

    if async_opts:
        parser.add_option('-P', '--poll', default=constants.DEFAULT_POLL_INTERVAL, type='int',
            dest='poll_interval',
            help="set the poll interval if using -B (default=%s)" % constants.DEFAULT_POLL_INTERVAL)
        parser.add_option('-B', '--background', dest='seconds', type='int', default=0,
            help='run asynchronously, failing after X seconds (default=N/A)')

    if check_opts:
        parser.add_option("-C", "--check", default=False, dest='check', action='store_true',
            help="don't make any changes; instead, try to predict some of the changes that may occur"
        )

    if diff_opts:
        parser.add_option("-D", "--diff", default=False, dest='diff', action='store_true',
            help="when changing (small) files and templates, show the differences in those files; works great with --check"
        )


    return parser

def ask_passwords(ask_pass=False, ask_sudo_pass=False):
    sshpass = None
    sudopass = None
    sudo_prompt = "sudo password: "

    if ask_pass:
        sshpass = getpass.getpass(prompt="SSH password: ")
        sudo_prompt = "sudo password [defaults to SSH password]: "

    if ask_sudo_pass:
        sudopass = getpass.getpass(prompt=sudo_prompt)
        if ask_pass and sudopass == '':
            sudopass = sshpass

    return (sshpass, sudopass)

def do_encrypt(result, encrypt, salt_size=None, salt=None):
    if PASSLIB_AVAILABLE:
        try:
            crypt = getattr(passlib.hash, encrypt)
        except:
            raise errors.AnsibleError("passlib does not support '%s' algorithm" % encrypt)

        if salt_size:
            result = crypt.encrypt(result, salt_size=salt_size)
        elif salt:
            result = crypt.encrypt(result, salt=salt)
        else:
            result = crypt.encrypt(result)
    else:
        raise errors.AnsibleError("passlib must be installed to encrypt vars_prompt values")

    return result

def last_non_blank_line(buf):

    all_lines = buf.splitlines()
    all_lines.reverse()
    for line in all_lines:
        if (len(line) > 0):
            return line
    # shouldn't occur unless there's no output
    return ""

def filter_leading_non_json_lines(buf):
    '''
    used to avoid random output from SSH at the top of JSON output, like messages from
    tcagetattr, or where dropbear spews MOTD on every single command (which is nuts).

    need to filter anything which starts not with '{', '[', ', '=' or is an empty line.
    filter only leading lines since multiline JSON is valid.
    '''

    filtered_lines = StringIO.StringIO()
    stop_filtering = False
    for line in buf.splitlines():
        if stop_filtering or "=" in line or line.startswith('{') or line.startswith('['):
            stop_filtering = True
            filtered_lines.write(line + '\n')
    return filtered_lines.getvalue()

def boolean(value):
    val = str(value)
    if val.lower() in [ "true", "t", "y", "1", "yes" ]:
        return True
    else:
        return False

def compile_when_to_only_if(expression):
    '''
    when is a shorthand for writing only_if conditionals.  It requires less quoting
    magic.  only_if is retained for backwards compatibility.
    '''

    # when: set $variable
    # when: unset $variable
    # when: failed $json_result
    # when: changed $json_result
    # when: int $x >= $z and $y < 3
    # when: int $x in $alist
    # when: float $x > 2 and $y <= $z
    # when: str $x != $y
    # when: jinja2_compare asdf  # implies {{ asdf }}

    if type(expression) not in [ str, unicode ]:
        raise errors.AnsibleError("invalid usage of when_ operator: %s" % expression)
    tokens = expression.split()
    if len(tokens) < 2:
        raise errors.AnsibleError("invalid usage of when_ operator: %s" % expression)

    # when_set / when_unset
    if tokens[0] in [ 'set', 'unset' ]:
        tcopy = tokens[1:]
        for (i,t) in enumerate(tokens[1:]):
            if t.find("$") != -1:
                tcopy[i] = "is_%s('''%s''')" % (tokens[0], t)
            else:
                tcopy[i] = t
        return " ".join(tcopy)

    # when_failed / when_changed
    elif tokens[0] in [ 'failed', 'changed' ]:
        tcopy = tokens[1:]
        for (i,t) in enumerate(tokens[1:]):
            if t.find("$") != -1:
                tcopy[i] = "is_%s(%s)" % (tokens[0], t)
            else:
                tcopy[i] = t
        return " ".join(tcopy)

    # when_integer / when_float / when_string
    elif tokens[0] in [ 'integer', 'float', 'string' ]:
        cast = None
        if tokens[0] == 'integer':
            cast = 'int'
        elif tokens[0] == 'string':
            cast = 'str'
        elif tokens[0] == 'float':
            cast = 'float'
        tcopy = tokens[1:]
        for (i,t) in enumerate(tokens[1:]):
            #if re.search(t, r"^\w"):
                # bare word will turn into Jinja2 so all the above
                # casting is really not needed
                #tcopy[i] = "%s('''%s''')" % (cast, t)
            t2 = t.strip()
            if (t2[0].isalpha() or t2[0] == '$') and cast == 'str' and t2 != 'in':
                tcopy[i] = "'%s'" % (t)
            else:
                tcopy[i] = t
        result = " ".join(tcopy)
        return result


    # when_boolean
    elif tokens[0] in [ 'bool', 'boolean' ]:
        tcopy = tokens[1:]
        for (i, t) in enumerate(tcopy):
            if t.find("$") != -1:
                tcopy[i] = "(is_set('''%s''') and '''%s'''.lower() not in ('false', 'no', 'n', 'none', '0', ''))" % (t, t)
        return " ".join(tcopy)

    # the stock 'when' without qualification (new in 1.2), assumes Jinja2 terms
    elif tokens[0] == 'jinja2_compare':
        return " ".join(tokens)
    else:
        raise errors.AnsibleError("invalid usage of when_ operator: %s" % expression)

def make_sudo_cmd(sudo_user, executable, cmd):
    """
    helper function for connection plugins to create sudo commands
    """
    # Rather than detect if sudo wants a password this time, -k makes
    # sudo always ask for a password if one is required.
    # Passing a quoted compound command to sudo (or sudo -s)
    # directly doesn't work, so we shellquote it with pipes.quote()
    # and pass the quoted string to the user's shell.  We loop reading
    # output until we see the randomly-generated sudo prompt set with
    # the -p option.
    randbits = ''.join(chr(random.randint(ord('a'), ord('z'))) for x in xrange(32))
    prompt = '[sudo via ansible, key=%s] password: ' % randbits
    success_key = 'SUDO-SUCCESS-%s' % randbits
    sudocmd = '%s -k && %s %s -S -p "%s" -u %s %s -c %s' % (
        C.DEFAULT_SUDO_EXE, C.DEFAULT_SUDO_EXE, C.DEFAULT_SUDO_FLAGS,
        prompt, sudo_user, executable or '$SHELL', pipes.quote('echo %s; %s' % (success_key, cmd)))
    return ('/bin/sh -c ' + pipes.quote(sudocmd), prompt, success_key)

_TO_UNICODE_TYPES = (unicode, type(None))

def to_unicode(value):
    if isinstance(value, _TO_UNICODE_TYPES):
        return value
    return value.decode("utf-8")

def get_diff(diff):
    # called by --diff usage in playbook and runner via callbacks
    # include names in diffs 'before' and 'after' and do diff -U 10

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            ret = []
            if 'dst_binary' in diff:
                ret.append("diff skipped: destination file appears to be binary\n")
            if 'src_binary' in diff:
                ret.append("diff skipped: source file appears to be binary\n")
            if 'dst_larger' in diff:
                ret.append("diff skipped: destination file size is greater than %d\n" % diff['dst_larger'])
            if 'src_larger' in diff:
                ret.append("diff skipped: source file size is greater than %d\n" % diff['src_larger'])
            if 'before' in diff and 'after' in diff:
                if 'before_header' in diff:
                    before_header = "before: %s" % diff['before_header']
                else:
                    before_header = 'before'
                if 'after_header' in diff:
                    after_header = "after: %s" % diff['after_header']
                else:
                    after_header = 'after'
                differ = difflib.unified_diff(to_unicode(diff['before']).splitlines(True), to_unicode(diff['after']).splitlines(True), before_header, after_header, '', '', 10)
                for line in list(differ):
                    ret.append(line)
            return u"".join(ret)
    except UnicodeDecodeError:
        return ">> the files are different, but the diff library cannot compare unicode strings"

def is_list_of_strings(items):
    for x in items:
        if not isinstance(x, basestring):
            return False
    return True

def safe_eval(str, locals=None, include_exceptions=False):
    '''
    this is intended for allowing things like:
    with_items: a_list_variable
    where Jinja2 would return a string
    but we do not want to allow it to call functions (outside of Jinja2, where
    the env is constrained)
    '''
    # FIXME: is there a more native way to do this?

    def is_set(var):
        return not var.startswith("$") and not '{{' in var

    def is_unset(var):
        return var.startswith("$") or '{{' in var

    # do not allow method calls to modules
    if not isinstance(str, basestring):
        # already templated to a datastructure, perhaps?
        if include_exceptions:
            return (str, None)
        return str
    if re.search(r'\w\.\w+\(', str):
        if include_exceptions:
            return (str, None)
        return str
    # do not allow imports
    if re.search(r'import \w+', str):
        if include_exceptions:
            return (str, None)
        return str
    try:
        result = None
        if not locals:
            result = eval(str)
        else:
            result = eval(str, None, locals)
        if include_exceptions:
            return (result, None)
        else:
            return result
    except Exception, e:
        if include_exceptions:
            return (str, e)
        return str


def listify_lookup_plugin_terms(terms, basedir, inject):

    if isinstance(terms, basestring):
        # someone did:
        #    with_items: alist
        # OR
        #    with_items: {{ alist }}

        stripped = terms.strip()
        if not (stripped.startswith('{') or stripped.startswith('[')) and not stripped.startswith("/"):
            # if not already a list, get ready to evaluate with Jinja2
            # not sure why the "/" is in above code :)
            try:
                new_terms = template.template(basedir, "{{ %s }}" % terms, inject)
                if isinstance(new_terms, basestring) and new_terms.find("{{") != -1:
                    pass
                else:
                    terms = new_terms
            except:
                pass

        if '{' in terms or '[' in terms:
            # Jinja2 already evaluated a variable to a list.
            # Jinja2-ified list needs to be converted back to a real type
            # TODO: something a bit less heavy than eval
            return safe_eval(terms)

        if isinstance(terms, basestring):
            terms = [ terms ]

    return terms

def deprecated(msg, version):
    ''' used to print out a deprecation message.'''
    if not C.DEPRECATION_WARNINGS:
        return
    new_msg = "\n[DEPRECATION WARNING]: %s. This feature will be removed in version %s." % (msg, version)
    new_msg = new_msg + " Deprecation warnings can be disabled by setting deprecation_warnings=False in ansible.cfg.\n\n"
    wrapped = textwrap.wrap(new_msg, 79)
    new_msg = "\n".join(wrapped) + "\n"

    if new_msg not in deprecations:
        display(new_msg, color='purple', stderr=True)
        deprecations[new_msg] = 1

def warning(msg):
    new_msg = "\n[WARNING]: %s" % msg
    wrapped = textwrap.wrap(new_msg, 79)
    new_msg = "\n".join(wrapped) + "\n"
    if new_msg not in warns:
        display(new_msg, color='bright purple', stderr=True)
        warns[new_msg] = 1

def combine_vars(a, b):

    if C.DEFAULT_HASH_BEHAVIOUR == "merge":
        return merge_hash(a, b)
    else:
        return dict(a.items() + b.items())

def random_password(length=20, chars=C.DEFAULT_PASSWORD_CHARS):
    '''Return a random password string of length containing only chars.'''

    password = []
    while len(password) < length:
        new_char = os.urandom(1)
        if new_char in chars:
            password.append(new_char)

    return ''.join(password)

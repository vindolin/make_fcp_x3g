import subprocess
import os
import sys
import time
import argparse
import re
import tempfile

VERSION = '20211215'

# Defaults. Each variable will be overridden if specified in the config file.
# If an array is specified in the file for a SINGLE-value item, only the first
# element of the array will be considered.
# In theory the script could be run without a config file at all, but I don't
# allow this because it would hide configuration mistakes.

EXTRA_PATH = ''
keep_orig = 0
debug = 0

GPX = ''

DUALSTRUDE_SCRIPT = []
PWM_SCRIPT = []
RETRACT_SCRIPT = []

Z_MAX = 150
FINAL_Z_MOVE = '; send Z axis to bottom of machine'
MACHINE = 'r1d'

conf_file = os.path.join(os.path.dirname(sys.argv[0]), 'make_fcp_x3g.txt')

parser = argparse.ArgumentParser(description="Processes G-code file for the FFCP and optionally converts it to X3G using GPX. Input file is overwritten and the X3G file is placed next to it, unless the SLIC3R_PP_OUTPUT_NAME environment variable exists. In the latter case, all additional files will be created based on the path indicated by that variable.")
parser.add_argument('-f', type=str, default=conf_file, help="Use custom config file. By default, the script looks for a file 'make_fcp_x3g.txt' in the same directory as the script. A config file is mandatory (it may be empty though).")
parser.add_argument('-c', action='store_true', help='Performs a sanity check on all configured paths, and warns if they do not point to executable files. (Nothing will be processed even if other arguments are passed.)')
parser.add_argument('-d', action='store_true', help="Debug mode: performs the sanity check, writes its result to a file 'make_fcp_x3g_check.txt' in the same directory as the input, and then continues processing. Will try to write a FAIL file in all cases where the script aborts unexpectedly.")
parser.add_argument('-w', action='store_true', help='Converts Windows file path to a Linux path inside a WSL environment.')
parser.add_argument('-P', action='store_true', help='Disables all postprocessing and only runs GPX without -p option.')
parser.add_argument('-p', action='store_true', help='Enable -p option of GPX even if -P is used.')
parser.add_argument('-k', action='store_true', help='Keep copy of original file.')
parser.add_argument('-s', type=int, help='Pause S seconds when exiting, useful for troubleshooting in Windows.')
parser.add_argument('-v', action='store_true', help='Verbose output.')
parser.add_argument('inputfile', type=str, help='Input file to be processed.')

args = parser.parse_args()

conf_file = args.f
sanity = args.c
wsl = args.w
no_postproc = args.P
force_progress = args.p
exit_sleep = args.s
verbose = args.v
debug = 1 if args.d else 0

# Try to parse input file argument already, such that we can at least try to
# write a FAIL file if -d and something fatal happens in the early stages.
inputfile = args.inputfile if len(args.inputfile) > 1 else None


# SUBROUTINES ####

def do_exit(code):
    if exit_sleep:
        time.sleep(exit_sleep)
    sys.exit(code)


def fatality(err, *msgs):
    print(*msgs, file=sys.stderr)
    if debug and fail_file:
        try:
            with open(fail_file, 'a') as f_handle:
                print(*msgs, file=f_handle)
        except IOError:
            pass
    do_exit(err)


def seppuku(*msgs):
    err = sys.exc_info()[1]
    err = os.WEXITSTATUS(os.waitpid(0, 0)[1]) if not err and os.WEXITSTATUS(os.waitpid(0, 0)[1]) else err
    err = 255 if not err else err
    fatality(err, *msgs)


def shell_escape(path):
    """
    Turns a file path argument into a double-quoted string that should be safe
    to use as a single unit in shell invocations.
    """
    if os.name == 'nt':
        # Fool-proof quoting of arguments in cmd.exe is pretty much
        # impossible, but if input is known to be a file path, then it should
        # suffice to wrap it between double quotes and escape any " inside it.
        path = path.replace('"', '\\"')
    else:
        # In UNIX-alikes, also wrap between double quotes and escape anything
        # that could be interpolated.
        path = re.sub(r'([\"`\\$])', r'\\\1', path)
    return f'"{path}"'


config_warnings = []


def read_config(f_path):
    item_single = {'KEEP_ORIG', 'DEBUG', 'EXTRA_PATH', 'GPX', 'Z_MAX', 'FINAL_Z_MOVE', 'MACHINE'}
    item_multiple = {'DUALSTRUDE_SCRIPT', 'PWM_SCRIPT', 'RETRACT_SCRIPT'}

    line_comment_re = re.compile(r'^\s*(#.*)?$')
    line_parse_re = re.compile(r'^\s*(\S+)\s*=\s*(.*?)\s*$')
    val_parse_re = re.compile(r'^("[^"]*"\s*)*$')
    val_findall_re = re.compile(r'"(.*?)"')

    try:
        with open(f_path, 'r') as f_handle:
            for n, line in enumerate(f_handle, 1):
                line = line.strip()
                if line_comment_re.match(line):
                    continue

                # Parse the line
                match = line_parse_re.match(line)
                if not match:
                    config_warnings.append(f"Ignored malformed line {n}.")
                    continue
                item, val = match.groups()
                vals = []
                if val_parse_re.match(val):
                    vals = val_findall_re.findall(val)
                else:
                    vals.append(val)
                    if '"' in val:
                        config_warnings.append(f"Double quote(s) found in value for '{item}' but could not parse as ARRAY, hence interpreted as SINGLE.")

                # Assign the variable.
                if item in item_single:
                    globals()[item] = vals[0] if vals else ''
                    if len(vals) > 1:
                        config_warnings.append(f"An array was specified for SINGLE item '{item}', only using first element.")
                elif item in item_multiple:
                    globals()[item] = vals
                else:
                    config_warnings.append(f"Ignored unknown item '{item}'.")
    except IOError:
        print(f"FATAL: cannot read {f_path}\nPut a readable configuration file at that path, or provide a different one with -f.")
        sys.exit(1)


def append_warning(msg):
    if verbose:
        print(f"Appending warnings:\n{msg}")
    try:
        with open(warn_file, 'a') as fh:
            print(f"{msg}", file=fh)
    except IOError:
        seppuku(f"FATAL: cannot write to {warn_file}: {sys.exc_info()[1]}")


def copy_file(in_kind, in_path, out_path):
    try:
        with open(in_path, 'rb') as i_handle, open(out_path, 'wb') as o_handle:
            while True:
                chunk = i_handle.read(32768)
                if not chunk:
                    break
                o_handle.write(chunk)
    except IOError as e:
        seppuku(f"FATAL: failed to read {in_kind} file '{in_path}': {e}" if 'rb' in e.modes else f"FATAL: failed to write to file '{out_path}': {e}")


def gpx_insane(o_handle):
    gpx_esc = shell_escape(GPX)
    if not (os.path.isfile(GPX) and os.access(GPX, os.X_OK)):
        print(f"Check failed: the 'GPX' path was specified but does not point to an executable file: {GPX}", file=o_handle)
        return 1
    try:
        subprocess.check_output(f"{gpx_esc} -? 2>&1", shell=True)
    except subprocess.CalledProcessError:
        print(f"Check failed: got unexpected result code when running gpx with -? argument: {sys.exc_info()[1]}", file=o_handle)
        return 1
    try:
        subprocess.check_output(f"echo T0 | {gpx_esc} -i -m \"{MACHINE}\" 2>&1", shell=True)
    except subprocess.CalledProcessError:
        print(f"Check failed: got error when running gpx with '{MACHINE}' machine type. Make sure this is supported. If not, try setting MACHINE to 'r1d'.", file=o_handle)
        return 1
    return 0


def postproc_script_insane(o_handle, name, script_config):
    for exc in script_config[0]:
        if not (os.path.isfile(exc) and os.access(exc, os.X_OK)):
            print(f"Check failed: the first element in the '{name}' list does not point to an executable file: {exc}", file=o_handle)
            return 1
        if len(script_config) > 1 and re.search(r'\.p[ly]$', script_config[1], re.I):
            if not (os.path.isfile(script_config[1]) and os.access(script_config[1], os.R_OK)):
                print(f"Check failed: the second element in the '{name}' list does not point to a readable script: {script_config[1]}", file=o_handle)
                return 1
        cmd = ' '.join(map(shell_escape, script_config))
        try:
            subprocess.check_output(f"{cmd} -h", shell=True, stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            print(f"Check failed: got unexpected result code when running {name} with -h argument.", file=o_handle)
            return 1
    return 0


def postproc_script_valid(*script_config):
    if not script_config:
        return 0

    for exc in script_config[0]:
        if not (os.path.isfile(exc) and os.access(exc, os.X_OK)):
            if verbose:
                print(f"'{exc}' is not (an) executable, ignoring.")
            return 0
        if len(script_config) > 1 and re.search(r'\.p[ly]$', script_config[1], re.I):
            if not (os.path.isfile(script_config[1]) and os.access(script_config[1], os.R_OK)):
                if verbose:
                    print(f"'{script_config[1]}' is not a readable script file, ignoring.")
                return 0
    return 1


def wsl_insane(o_handle):
    if not os.path.isfile('/proc/version'):
        return 0
    with open('/proc/version', 'r') as proc_f:
        proc_version = proc_f.readlines()
    if not any('Microsoft' in line or 'WSL' in line for line in proc_version):
        return 0
    if verbose:
        print("WSL detected")
    try:
        subprocess.check_output(['wslpath', '-a', 'C:\\Test\\file.zip']).decode().strip()
    except subprocess.CalledProcessError:
        print("Check failed: you seem to be inside a WSL environment but the 'wslpath' command is not available or is broken.", file=o_handle)
        print("Make sure you have at least Windows 10 version 1803, and 'wslpath' can be run from a bash shell.", file=o_handle)
        return 1
    return 0


def sanity_check(o_handle=None):
    if o_handle is None:
        o_handle = sys.stderr

    # print(f"Running sanity check for script version {VERSION}.\nPATH is:\n{os.environ['PATH']}\n", file=o_handle)

    if 'SLIC3R_PP_OUTPUT_NAME' in os.environ:
        print(f"SLIC3R_PP_OUTPUT_NAME is defined:\n{os.environ['SLIC3R_PP_OUTPUT_NAME']}\n", file=o_handle)
    fail = False
    if GPX:
        fail = gpx_insane(o_handle)
    if DUALSTRUDE_SCRIPT:
        fail = postproc_script_insane(o_handle, 'DUALSTRUDE_SCRIPT', DUALSTRUDE_SCRIPT)
    if PWM_SCRIPT:
        fail = postproc_script_insane(o_handle, 'PWM_SCRIPT', PWM_SCRIPT)
    if RETRACT_SCRIPT:
        fail = postproc_script_insane(o_handle, 'RETRACT_SCRIPT', RETRACT_SCRIPT)
    if os.path.isfile('/proc/version'):
        fail = wsl_insane(o_handle)

    if config_warnings:
        print(f"WARNING: found the following suspect things in the configuration file at '{conf_file}'. Please check the correctness of that file.", file=o_handle)
        print('\n'.join(config_warnings), file=o_handle)
    else:
        if not fail:
            print("All checks seem OK!", file=o_handle)


def run_script(name, gcode, cmd):
    print(f"Running {name} script...")
    tmpname = tempfile.mktemp()
    cmd = ' '.join(map(shell_escape, cmd))
    tmpname_esc = shell_escape(tmpname)
    gcode_esc = shell_escape(gcode)

    if verbose:
        print(f"Executing: {cmd} -o {tmpname_esc} {gcode_esc}")
    try:
        warnings = subprocess.check_output(f"{cmd} -o {tmpname_esc} {gcode_esc} 2>&1", shell=True, stderr=subprocess.STDOUT).decode()
    except subprocess.CalledProcessError as e:
        warnings = f"The {name} script failed ({e.returncode}), but without any output." if not e.output else e.output.decode()
        with open(fail_file, 'a') as o_handle:
            print(warnings, file=o_handle)
        seppuku(f"FATAL: running {name} script failed, aborting postprocessing.")
    copy_file('temporary', tmpname, gcode)
    os.remove(tmpname)

    if warnings:
        append_warning(warnings)


def adjust_final_z():
    # Finds the highest Z value in a G1 command from the last 2048 lines of
    # inputfile, and if it is higher than the command in the line containing
    # FINAL_Z_MOVE, it updates that line to prevent the move from ramming the
    # nozzle into the print.
    try:
        with open(inputfile, 'r+') as f_handle:
            lines = f_handle.readlines()
            lines = lines[-2048:]  # get last 2048 lines

            highest_z = -1
            final_z = -1
            final_index = -1

            pattern1 = re.compile(r'^G1 [^;]*Z(\d*\.?\d+)')
            pattern2 = re.compile(r'^G1 [^;]*Z(\d*\.?\d+).*' + re.escape(FINAL_Z_MOVE))
            pattern3 = re.compile(r'^(G1 [^;]*Z)\d*\.?\d+(.*)$')

            for i, line in enumerate(lines):

                match = pattern1.search(line)
                if match:
                    z = float(match.group(1))
                    highest_z = max(highest_z, z)

                match = pattern2.search(line)
                if match:
                    final_z = float(match.group(1))
                    final_index = i

            if verbose:
                print(f"Highest Z coordinate found: {highest_z}")
            if highest_z == -1:
                append_warning('WARNING: could not find highest Z coordinate. If this is a valid G-code file, the make_fcp_x3g script needs updating.')
                return
            if highest_z > int(Z_MAX):
                append_warning(f"WARNING: Z coordinates in this file exceed the maximum: {highest_z} > {Z_MAX}. This print will likely end in disaster.")

            updated = False

            if highest_z > final_z:
                if verbose:
                    print("Updating final Z move")
                # Update the line in memory
                lines[final_index] = pattern3.sub(rf'\1{highest_z}\2 ; EXTENDED!', lines[final_index])
                updated = True

            # After the loop, write the changes to the file
            if updated:
                with open(inputfile, 'w') as f_handle:
                    f_handle.writelines(lines)

    except IOError:
        seppuku(f"FATAL: cannot open '{inputfile}' for reading+writing: {os.strerror()}")

# Code below is executed when the script is run as a standalone program.


if wsl and inputfile is not None and inputfile != '':
    # Although the conversion between Windows and Linux paths seems trivial, it
    # has many quirks so it is better to rely on the dedicated wslpath tool.
    if verbose:
        print(f"Converting incoming Windows path '{inputfile}' to UNIX path")
    in_esc = shell_escape(inputfile)
    inputfile = subprocess.check_output(f'wslpath -a {in_esc}', shell=True).decode().strip()
    if inputfile == '':
        seppuku("FATAL: 'wslpath' command not found or failed")
    print(f"Converted Windows path to WSL path: '{inputfile}'")

# In case of WSL, this variable must already be converted to a Linux path.
outputfile = os.environ.get('SLIC3R_PP_OUTPUT_NAME', inputfile)

out_base = outputfile
if out_base is not None:
    out_base = os.path.splitext(out_base)[0]

origfile = f"{out_base}_orig.gcode" if out_base is not None else None
# Avoid making a possibly invisible file
out_base = 'make_fcp_x3g' if out_base == '' else out_base
warn_file = f"{out_base}.WARN.txt" if out_base is not None else None
fail_file = f"{out_base}.FAIL.txt" if out_base is not None else None

if out_base is not None:
    if os.path.exists(warn_file):
        os.remove(warn_file)
    if os.path.exists(fail_file):
        os.remove(fail_file)


if conf_file:
    read_config(conf_file)

# If config changed these, command line arguments still get precedence
if args.k:
    keep_orig = 1
if args.d:
    debug = 1

if exit_sleep is not None and not re.match(r'^\d?\.?\d+$', str(exit_sleep)):
    # Since someone is probably trying to add the -s argument to catch an
    # error briefly flashing, sleep with a default to show this error.
    exit_sleep = 3
    fatality(2, "ERROR: argument following -s must be a positive number")

if EXTRA_PATH:
    if os.name == 'nt':
        os.environ['PATH'] = f"{EXTRA_PATH};{os.environ['PATH']}"
    elif re.search(r'(:|^)/usr/bin:', os.environ['PATH']):
        os.environ['PATH'] = re.sub(r'(:|^)/usr/bin:', r'\1' + EXTRA_PATH + ':/usr/bin:', os.environ['PATH'])
    else:
        os.environ['PATH'] = f"{EXTRA_PATH}:{os.environ['PATH']}"

if not inputfile or inputfile == '':
    fatality(2, "ERROR: argument should be the path to a .gcode file.\nRun this script with -h for usage information.")

if debug:
    check_out = os.path.join(os.path.dirname(outputfile), 'make_fcp_x3g_check.txt')
    try:
        with open(check_out, 'w') as o_handle:
            sanity_check()
    except IOError:
        seppuku(f"FATAL: cannot write to '{check_out}': {sys.exc_info()[1]}")

if not os.path.isfile(inputfile):
    fatality(2, f"ERROR: input file not found or is not a file: {inputfile}")
if not os.access(inputfile, os.R_OK):
    fatality(2, f"ERROR: input file not readable, maybe insufficient permissions: {inputfile}")

# -p in GPX overrides % display with something that better approximates total
# print time than merely mapping the Z coordinate to a percentage. It still is
# not perfect but at least gives a sensible ballpark figure. For this to work
# properly, cargo cult folklore says that the start GCode block must end with
# "M73 P1 ;@body", although a peek in GPX source code reveals that either
# "M73 P1" or @body will work.

arg_p = '-p' if force_progress else ''

if not no_postproc:
    arg_p = '-p'

    if keep_orig:
        copy_file('original', inputfile, origfile)

    if FINAL_Z_MOVE:
        adjust_final_z()

    dualstrude = left_right = m104_seen = m83_seen = fix_m104 = False
    with open(inputfile, 'rb') as i_handle:
        for line in i_handle:
            line = line.decode()
            if not dualstrude and re.match(r'^;- - - Custom G-code for dual extruder printing', line):
                dualstrude = True
            if not left_right and re.match(r'^;- - - Custom G-code for (left|right) extruder printing', line):
                left_right = True
            if not m104_seen and re.match(r'^M104 S.+ T.+; set temperature$', line):
                m104_seen = True
            if not m83_seen and re.match(r'^M83(;|\s|$)', line):
                m83_seen = True

    if dualstrude and postproc_script_valid(DUALSTRUDE_SCRIPT):
        run_script('dualstrusion', inputfile, DUALSTRUDE_SCRIPT)
    elif left_right and m104_seen:
        fix_m104 = True

    if fix_m104 or m83_seen:
        print("Fixing incorrect M104 command for single-extrusion setup") if fix_m104 else None
        print("Ensuring correct display in gcode.ws") if m83_seen else None
        with tempfile.NamedTemporaryFile(delete=False) as o_handle:
            tmpname = o_handle.name
            with open(inputfile, 'rb') as i_handle:
                for line in i_handle:
                    line = line.decode()
                    if fix_m104:
                        line = re.sub(r'^M104 S(\S+) (T.*); set temperature$', r'M104 S\1 ; POSTPROCESS FIX: \2 ARGUMENT REMOVED', line)
                    if m83_seen:
                        line = re.sub(r'^(G90 ; use absolute coordinates)$', r'\1\nM83; POSTPROCESS workaround for relative E in gcode.ws', line)
                    o_handle.write(line.encode())
        copy_file('temporary', tmpname, inputfile)
        os.unlink(tmpname)

    # if postproc_script_valid(RETRACT_SCRIPT):
    #     run_script('retraction', inputfile, *RETRACT_SCRIPT)

    # if postproc_script_valid(PWM_SCRIPT):
    #     run_script('fan PWM post-processing', inputfile, *PWM_SCRIPT)


if os.path.isfile(GPX) and os.access(GPX, os.X_OK):
    print("Invoking GPX...")
    gpx_esc = shell_escape(GPX)
    in_esc = shell_escape(inputfile)
    out_esc = shell_escape(outputfile.rsplit('.gcode', 1)[0] + '.x3g')
    if verbose:
        print(f"Executing: {gpx_esc} {arg_p} -m \"{MACHINE}\" {in_esc} {out_esc}")
    gpx_out = subprocess.check_output(f"{gpx_esc} {arg_p} -m \"{MACHINE}\" {in_esc} {out_esc} 2>&1", shell=True)
    if verbose and gpx_out:
        print(gpx_out)

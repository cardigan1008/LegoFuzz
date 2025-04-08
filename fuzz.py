#!/usr/bin/env python3
import re
import sys
import os, shutil, time, tempfile, signal, random, string
from datetime import datetime
from glob import glob
from enum import Enum, auto
from diopter.compiler import (
    CompilationSetting,
    CompilerExe,
    OptLevel,
    SourceProgram,
    Language,
)
from diopter.sanitizer import Sanitizer
from diopter.utils import TempDirEnv
import subprocess as sp
from synthesize import Synthesizer
from utils.compcert import CComp as this_CComp
from pathlib import Path

DEBUG = 0
"""CONFIG"""
FUNCTION_DB_FILE = os.path.join(os.path.dirname(__file__), './functions.json')
NUM_MUTANTS = 10 # number of mutants generated by the synthesizer per seed.
COMPILER_TIMEOUT = 200
PROG_TIMEOUT = 10
CCOMP_TIMEOUT = 60 # compcert timeout
"""TOOL"""
CSMITH_HOME = os.environ["CSMITH_HOME"]
CC = CompilationSetting(
            compiler=CompilerExe.get_system_gcc(),
            opt_level=OptLevel.O3,
            flags=("-march=native",f"-I{CSMITH_HOME}/include"),
            )
SAN_SAN = Sanitizer(checked_warnings=False, use_ub_address_sanitizer=True, use_ccomp_if_available=False, debug=DEBUG) # sanitizers only
SAN_CCOMP = this_CComp.get_system_ccomp() # CompCert only


"""Global vars"""

WORK_DIR = "work"
if not os.path.exists(WORK_DIR):
    os.makedirs(WORK_DIR)

class CompCode(Enum):
    """Compile status
    """
    OK      =   auto()  # ok
    Timeout =   auto()  # timeout during compilation
    Sanfail =   auto()  # sanitization failed
    Crash   =   auto()  # compiler crash
    Error   =   auto()  # compiler error
    WrongEval=  auto()  # inconsistent results across compilers but consistent within the same compiler
    Wrong   =   auto()  # inconsistent results across compilers/opts

def generate_random_string(len:int=5) -> str:
    """Generate a random string of length len"""
    return ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(len))

def run_cmd(cmd, timeout):
    if type(cmd) is not list:
        cmd = cmd.split(' ')
        cmd = list(filter(lambda x: x!='', cmd))
    # Start the subprocess
    process = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
    # Wait for the subprocess to finish or timeout
    try:
        output, error = process.communicate(timeout=timeout)
        output = output.decode("utf-8")
    except sp.TimeoutExpired:
        # Timeout occurred, kill the process
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        finally:
            output = ''
        # A workaround to tmpxxx.exe as it sometimes escapes from os.killpg
        cmd_str = " ".join(cmd)
        if '.exe' in cmd_str:
            os.system(f"pkill -9 -f {cmd_str}")
        return 124, output

    # Return the exit code and stdout of the process
    return process.returncode, output

def write_bug_desc_to_file(to_file, data):
    with open(to_file, "a") as f:
        f.write(f"/* {data} */\n")

def read_checksum(data):
    res = re.findall(r'checksum = (.*)', data)
    if len(res) > 0:
        return res[0]
    return 'NO_CKSUM'

def check_sanitizers(src):
    """Check validity with sanitizers"""
    with open(src, 'r') as f:
        code = f.read()
    prog = SourceProgram(code=code, language=Language.C)
    preprog = CC.preprocess_program(prog, make_compiler_agnostic=True)
    if DEBUG:
        print(datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "SAN.sanitize", flush=True)
    if not SAN_SAN.sanitize(preprog):
        return False
    return True

def check_ccomp(src, random_count=1):
    """
    Check validity with CompCert.
    src:str -> source file
    random_count:int -> the number of times using ccomp -random for checking
    """
    with open(src, 'r') as f:
        code = f.read()
    prog = SourceProgram(code=code, language=Language.C)
    preprog = CC.preprocess_program(prog, make_compiler_agnostic=True)
    if DEBUG:
        print(datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "SAN.ccomp", flush=True)
    with TempDirEnv():
        try:
            ccomp_result = SAN_CCOMP.check_program(preprog, timeout=CCOMP_TIMEOUT, additional_flags=["-fstruct-passing"], debug=DEBUG)
        except sp.TimeoutExpired:
            return False
        if ccomp_result is False:
            return False
    with TempDirEnv():
        for _ in range(random_count):
            try:
                ccomp_result_random = SAN_CCOMP.check_program(preprog, timeout=CCOMP_TIMEOUT, debug=DEBUG, additional_flags=["-fstruct-passing", "-random"])
            except sp.TimeoutExpired:
                return False
            if ccomp_result_random is False:
                return False
            # check for unspecified behavior
            if ccomp_result.stdout != ccomp_result_random.stdout:
                return False
    return True

def compile_and_run(compiler, src):
    cksum = ''
    tmp_f = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
    tmp_f.close()
    exe = tmp_f.name
    cmd = f"{compiler} {src} -I{CSMITH_HOME}/include -o {exe}"
    ret, out = run_cmd(cmd, COMPILER_TIMEOUT)
    if ret == 124: # another compile chance when timeout
        time.sleep(1)
        ret, out = run_cmd(cmd, COMPILER_TIMEOUT)
    if ret == 124: # we treat timeout as crash now.
        write_bug_desc_to_file(src, f"Compiler timeout! Can't compile with {compiler}")
        if os.path.exists(exe): os.remove(exe)
        return CompCode.Timeout, cksum
    if ret != 0:
        write_bug_desc_to_file(src, f"Compiler crash! Can't compile with {compiler}")
        if os.path.exists(exe): os.remove(exe)
        return CompCode.Crash, cksum
    ret, out = run_cmd(f"{exe}", PROG_TIMEOUT)
    cksum = read_checksum(out)
    write_bug_desc_to_file(src, f"EXITof {compiler}: {ret}")
    write_bug_desc_to_file(src, f"CKSMof {compiler}: {cksum}")
    if os.path.exists(exe): os.remove(exe)
    return CompCode.OK, cksum

def check_compile(src:str, compilers:list) -> CompCode:
    """Compile the program with a list of compilers and check their status
    """
    cksum_list = []
    for comp in compilers:
        if DEBUG:
            print(datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "compiler_and_run: ", comp, flush=True)
        ret, cksum = compile_and_run(comp, src)
        if ret == CompCode.Crash:
            return CompCode.Crash
        if ret == CompCode.Timeout:
            return CompCode.Timeout
        if ret != CompCode.OK:
            return CompCode.Error
        cksum_list.append(cksum)
    if len(cksum_list) != len(compilers) or len(set(cksum_list)) != 1:
        maybe_WrongEval = True
        for i in range(len(compilers)):
            for j in range(i+1, len(compilers)):
                if compilers[i].split(' ')[0] == compilers[j].split(' ')[0] and cksum_list[i] != cksum_list[j]:
                    maybe_WrongEval = False
        if maybe_WrongEval:
            return CompCode.WrongEval
        return CompCode.Wrong
    return CompCode.OK

def run_one(compilers:list[str], save_wrong_dir:Path, SYNER:Synthesizer) -> Path | None:
    """Run compiler testing
    """
    # synthesize
    if DEBUG:
        print(datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "Start synthesizer", flush=True)
    try:
        current_dir = Path.cwd() / WORK_DIR
        _, syn_files, _ = SYNER.synthesizer(dst_dir=current_dir)
    except Exception as e: 
        print(f'SynthesizerError: {e}')
        return None
    src = syn_files[0]
    for syn_f in syn_files[1:]:
        print(f"Synthesized program {syn_f}", flush=True)
        ret = check_compile(syn_f, compilers)
        if ret == CompCode.WrongEval:
            print(f"COMPILER BUG FOUND but may due to evaluation order difference\n", flush=True)
        if ret == CompCode.Wrong:
            if not check_sanitizers(syn_f) or not check_ccomp(syn_f, random_count=30):
                print(f"Inconsistent output due to UB {syn_f}", flush=True)
                continue
            rand_name = generate_random_string(8)
            case_dir = save_wrong_dir / f"case_{rand_name}"
            case_dir.mkdir(parents=True, exist_ok=True)
            wrong_file = case_dir / "case.c"
            orig_file = case_dir / "orig.c"
            print(f"COMPILER BUG FOUND: id {rand_name}\n", flush=True)
            shutil.copyfile(syn_f, wrong_file.absolute().as_posix())
            shutil.copyfile(src, orig_file.absolute().as_posix())
            for syn_f in syn_files:
                os.remove(syn_f)
            return wrong_file
        if ret == CompCode.Crash:
            rand_name = generate_random_string(8)
            case_dir = save_wrong_dir / f"case_{rand_name}"
            case_dir.mkdir(parents=True, exist_ok=True)
            crash_file = case_dir / "case.c"
            orig_file = case_dir / "orig.c"
            print(f"COMPILER CRASH FOUND: id {rand_name}\n", flush=True)
            shutil.copyfile(syn_f, crash_file.absolute().as_posix())
            shutil.copyfile(src, orig_file.absolute().as_posix())
            for syn_f in syn_files:
                os.remove(syn_f)
            return crash_file
    if DEBUG:
        print(datetime.now().strftime("%d/%m/%Y %H:%M:%S"), "End synthesizer", flush=True)
    for syn_f in syn_files:
        os.remove(syn_f)
    return None

def parse_compilers(compiler_config_file):
    with open(compiler_config_file, 'r') as f:
        lines = f.readlines()
    compilers = []
    for line in lines:
        line = line.strip()
        if line == '':
            continue
        with tempfile.NamedTemporaryFile(suffix=".c", mode="w", delete=False) as tmp_f:
            tmp_f.write("int main() { return 0;}")
            tmp_f.close()
            test_compiler_cmd = f"{line} {tmp_f.name} -o /dev/null"
            ret, out = run_cmd(test_compiler_cmd, COMPILER_TIMEOUT)
            os.remove(tmp_f.name)
            if ret != 0:
                exit(f"cannot execute compiler {line}")
            else:
                compilers.append(line)
    return compilers


if __name__=='__main__':
    SAVE_DIR = Path(__file__).parent / "work/wrong"
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    compilers = parse_compilers(sys.argv[1])
    SYNER = Synthesizer(func_database=FUNCTION_DB_FILE, prob=80, num_mutant=NUM_MUTANTS, iter=200, RAND=True, INLINE=False, DEBUG=DEBUG)
    with TempDirEnv() as tmp_dir:
        os.environ['TMPDIR'] = tmp_dir.absolute().as_posix()
        while 1:
            run_one(compilers, SAVE_DIR, SYNER)
            print("---")
            for p in tmp_dir.iterdir():
                p.unlink()

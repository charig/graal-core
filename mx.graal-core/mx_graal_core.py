#
# ----------------------------------------------------------------------------------------------------
#
# Copyright (c) 2007, 2015, Oracle and/or its affiliates. All rights reserved.
# DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS FILE HEADER.
#
# This code is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 only, as
# published by the Free Software Foundation.
#
# This code is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# version 2 for more details (a copy is included in the LICENSE file that
# accompanied this code).
#
# You should have received a copy of the GNU General Public License version
# 2 along with this work; if not, write to the Free Software Foundation,
# Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Please contact Oracle, 500 Oracle Parkway, Redwood Shores, CA 94065 USA
# or visit www.oracle.com if you need additional information or have any
# questions.
#
# ----------------------------------------------------------------------------------------------------

import os
from os.path import join, exists, getmtime
from argparse import ArgumentParser
import re
import zipfile
import subprocess

import mx
from mx_gate import Task

from mx_unittest import unittest
from mx_javamodules import as_java_module
import mx_gate
import mx_unittest
import mx_microbench

import mx_graal_benchmark # pylint: disable=unused-import
import mx_graal_tools #pylint: disable=unused-import

_suite = mx.suite('graal-core')

""" Prefix for running the VM. """
_vm_prefix = None

def get_vm_prefix(asList=True):
    """
    Get the prefix for running the VM (e.g. "gdb --args").
    """
    if asList:
        return _vm_prefix.split() if _vm_prefix is not None else []
    return _vm_prefix

#: The JDK used to build and run Graal.
jdk = mx.get_jdk(tag='default')

if jdk.javaCompliance < '1.8':
    mx.abort('Graal requires JDK8 or later, got ' + str(jdk))

#: Specifies if Graal is being built/run against JDK8. If false, then
#: JDK9 or later is being used (checked above).
isJDK8 = jdk.javaCompliance < '1.9'

def _check_jvmci_version(jdk):
    """
    Runs a Java utility to check that `jdk` supports the minimum JVMCI API required by Graal.
    """
    simplename = 'JVMCIVersionCheck'
    name = 'com.oracle.graal.hotspot.' + simplename
    binDir = mx.ensure_dir_exists(join(_suite.get_output_root(), '.jdk' + str(jdk.version)))
    if isinstance(_suite, mx.BinarySuite):
        javaSource = join(binDir, simplename + '.java')
        if not exists(javaSource):
            dists = [d for d in _suite.dists if d.name == 'GRAAL_HOTSPOT']
            assert len(dists) == 1, 'could not find GRAAL_HOTSPOT distribution'
            d = dists[0]
            assert exists(d.sourcesPath), 'missing expected file: ' + d.sourcesPath
            with zipfile.ZipFile(d.sourcesPath, 'r') as zf:
                with open(javaSource, 'w') as fp:
                    fp.write(zf.read(name.replace('.', '/') + '.java'))
    else:
        javaSource = join(_suite.dir, 'graal', 'com.oracle.graal.hotspot', 'src', name.replace('.', '/') + '.java')
    javaClass = join(binDir, name.replace('.', '/') + '.class')
    if not exists(javaClass) or getmtime(javaClass) < getmtime(javaSource):
        mx.run([jdk.javac, '-d', binDir, javaSource])
    mx.run([jdk.java, '-cp', binDir, name])

_check_jvmci_version(jdk)

class JVMCIClasspathEntry(object):
    """
    Denotes a distribution that is put on the JVMCI class path.

    :param str name: the name of the `JARDistribution` to be deployed
    """
    def __init__(self, name):
        self._name = name

    def dist(self):
        """
        Gets the `JARDistribution` deployed on the JVMCI class path.
        """
        return mx.distribution(self._name)

    def get_path(self):
        """
        Gets the path to the distribution jar file.

        :rtype: str
        """
        return self.dist().classpath_repr()

#: The deployed Graal distributions
_jvmci_classpath = [
    JVMCIClasspathEntry('GRAAL'),
]

def add_jvmci_classpath_entry(entry):
    """
    Appends an entry to the JVMCI classpath.
    """
    _jvmci_classpath.append(entry)

_bootclasspath_appends = []

def add_bootclasspath_append(dep):
    """
    Adds a distribution that must be appended to the boot class path
    """
    assert dep.isJARDistribution(), dep.name + ' is not a distribution'
    _bootclasspath_appends.append(dep)

mx_gate.add_jacoco_includes(['com.oracle.graal.*'])
mx_gate.add_jacoco_excluded_annotations(['@Snippet', '@ClassSubstitution'])

class JVMCIMicrobenchExecutor(mx_microbench.MicrobenchExecutor):

    def parseVmArgs(self, vmArgs):
        if isJDK8:
            if _is_jvmci_enabled(vmArgs) and '-XX:-UseJVMCIClassLoader' not in vmArgs:
                vmArgs = ['-XX:-UseJVMCIClassLoader'] + vmArgs
        return ['-server'] + _parseVmArgs(vmArgs)

    def parseForkedVmArgs(self, vmArgs):
        return ['-server'] + _parseVmArgs(vmArgs)

    def run_java(self, args):
        return run_vm(args)

mx_microbench.set_microbenchmark_executor(JVMCIMicrobenchExecutor())

def _get_XX_option_value(vmargs, name, default):
    """
    Gets the value of an ``-XX:`` style HotSpot VM option.

    :param list vmargs: VM arguments to inspect
    :param str name: the name of the option
    :param default: the default value of the option if it's not present in `vmargs`
    :return: the value of the option as specified in `vmargs` or `default`
    """
    for arg in reversed(vmargs):
        if arg == '-XX:-' + name:
            return False
        if arg == '-XX:+' + name:
            return True
        if arg.startswith('-XX:' + name + '='):
            return arg[len('-XX:' + name + '='):]
    return default

def _is_jvmci_enabled(vmargs):
    """
    Determines if JVMCI is enabled according to the given VM arguments and whether JDK > 8.

    :param list vmargs: VM arguments to inspect
    """
    return _get_XX_option_value(vmargs, 'EnableJVMCI', isJDK8)

def ctw(args, extraVMarguments=None):
    """run CompileTheWorld"""

    defaultCtwopts = 'Inline=false'

    parser = ArgumentParser(prog='mx ctw')
    parser.add_argument('--ctwopts', action='store', help='space separated JVMCI options used for CTW compilations (default: --ctwopts="' + defaultCtwopts + '")', default=defaultCtwopts, metavar='<options>')
    parser.add_argument('--cp', '--jar', action='store', help='jar or class path denoting classes to compile', metavar='<path>')
    if not isJDK8:
        parser.add_argument('--limitmods', action='store', help='limits the set of compiled classes to only those in the listed modules', metavar='<modulename>[,<modulename>...]')

    args, vmargs = parser.parse_known_args(args)

    if args.ctwopts:
        # Replace spaces with '#' since it cannot contain spaces
        vmargs.append('-Dgraal.CompileTheWorldConfig=' + re.sub(r'\s+', '#', args.ctwopts))

    # suppress menubar and dock when running on Mac; exclude x11 classes as they may cause VM crashes (on Solaris)
    vmargs = ['-Djava.awt.headless=true'] + vmargs

    if args.cp:
        cp = os.path.abspath(args.cp)
        if not isJDK8 and not _is_jvmci_enabled(vmargs):
            mx.abort('Non-Graal CTW does not support specifying a specific class path or jar to compile')
    else:
        if isJDK8:
            cp = join(jdk.home, 'jre', 'lib', 'rt.jar')
        else:
            # Compile all classes in the JRT image by default.
            cp = join(jdk.home, 'lib', 'modules')

    vmargs.append('-Dgraal.CompileTheWorldExcludeMethodFilter=sun.awt.X11.*.*')

    if _get_XX_option_value(vmargs + _noneAsEmptyList(extraVMarguments), 'UseJVMCICompiler', False):
        vmargs.append('-XX:+BootstrapJVMCI')

    if isJDK8:
        if not _is_jvmci_enabled(vmargs):
            vmargs.extend(['-XX:+CompileTheWorld', '-Xbootclasspath/p:' + cp])
        else:
            vmargs.extend(['-Dgraal.CompileTheWorldClasspath=' + cp, '-XX:-UseJVMCIClassLoader', 'com.oracle.graal.hotspot.CompileTheWorld'])
    else:
        if _is_jvmci_enabled(vmargs):
            # To be able to load all classes in the JRT with Class.forName,
            # all JDK modules need to be made root modules.
            limitmods = frozenset(args.limitmods.split(',')) if args.limitmods else None
            nonBootJDKModules = [m.name for m in jdk.get_modules() if not m.boot and (limitmods is None or m.name in limitmods)]
            if nonBootJDKModules:
                vmargs.append('--add-modules=' + ','.join(nonBootJDKModules))
            if args.limitmods:
                vmargs.append('-DCompileTheWorld.limitmods=' + args.limitmods)
            vmargs.extend(['-Dgraal.CompileTheWorldClasspath=' + cp, 'com.oracle.graal.hotspot.CompileTheWorld'])
        else:
            vmargs.append('-XX:+CompileTheWorld')

    run_vm(vmargs + _noneAsEmptyList(extraVMarguments))

class UnitTestRun:
    def __init__(self, name, args, tags):
        self.name = name
        self.args = args
        self.tags = tags

    def run(self, suites, tasks, extraVMarguments=None):
        for suite in suites:
            with Task(self.name + ': hosted-product ' + suite, tasks, tags=self.tags) as t:
                if mx_gate.Task.verbose:
                    extra_args = ['--verbose', '--enable-timing']
                else:
                    extra_args = []
                if t: unittest(['--suite', suite, '--fail-fast'] + extra_args + self.args + _noneAsEmptyList(extraVMarguments))

class BootstrapTest:
    def __init__(self, name, args, tags, suppress=None):
        self.name = name
        self.args = args
        self.suppress = suppress
        self.tags = tags

    def run(self, tasks, extraVMarguments=None):
        with Task(self.name, tasks, tags=self.tags) as t:
            if t:
                if self.suppress:
                    out = mx.DuplicateSuppressingStream(self.suppress).write
                else:
                    out = None
                run_vm(self.args + ['-XX:+UseJVMCICompiler'] + _noneAsEmptyList(extraVMarguments) + ['-XX:-TieredCompilation', '-XX:+BootstrapJVMCI', '-version'], out=out)

class MicrobenchRun:
    def __init__(self, name, args, tags):
        self.name = name
        self.args = args
        self.tags = tags

    def run(self, tasks, extraVMarguments=None):
        with Task(self.name + ': hosted-product ', tasks, tags=self.tags) as t:
            if t: mx_microbench.get_microbenchmark_executor().microbench(_noneAsEmptyList(extraVMarguments) + ['--', '-foe', 'true'] + self.args)

class GraalTags:
    bootstrap = ['bootstrap', 'fulltest']
    bootstraplite = ['bootstraplite', 'bootstrap', 'fulltest']
    test = ['test', 'fulltest']
    benchmarktest = ['benchmarktest', 'fulltest']
    ctw = ['ctw', 'fulltest']

def _noneAsEmptyList(a):
    if not a or not any(a):
        return []
    return a

def _gate_java_benchmark(args, successRe):
    """
    Runs a Java benchmark and aborts if the benchmark process exits with a non-zero
    exit code or the `successRe` pattern is not in the output of the benchmark process.

    :param list args: the arguments to pass to the VM
    :param str successRe: a regular expression
    """
    out = mx.OutputCapture()
    try:
        run_java(args, out=mx.TeeOutputCapture(out), err=subprocess.STDOUT)
    finally:
        jvmErrorFile = re.search(r'(([A-Z]:|/).*[/\]hs_err_pid[0-9]+\.log)', out.data)
        if jvmErrorFile:
            jvmErrorFile = jvmErrorFile.group()
            mx.log('Dumping ' + jvmErrorFile)
            with open(jvmErrorFile, 'rb') as fp:
                mx.log(fp.read())
            os.unlink(jvmErrorFile)

    if not re.search(successRe, out.data, re.MULTILINE):
        mx.abort('Could not find benchmark success pattern: ' + successRe)

def _gate_dacapo(name, iterations, extraVMarguments=None):
    vmargs = ['-Xms2g', '-XX:+UseSerialGC', '-XX:-UseCompressedOops', '-Djava.net.preferIPv4Stack=true', '-Dgraal.ExitVMOnException=true'] + _noneAsEmptyList(extraVMarguments)
    dacapoJar = mx.library('DACAPO').get_path(True)
    _gate_java_benchmark(vmargs + ['-jar', dacapoJar, name, '-n', str(iterations)], r'^===== DaCapo 9\.12 ([a-zA-Z0-9_]+) PASSED in ([0-9]+) msec =====')

def _gate_scala_dacapo(name, iterations, extraVMarguments=None):
    vmargs = ['-Xms2g', '-XX:+UseSerialGC', '-XX:-UseCompressedOops', '-Dgraal.ExitVMOnException=true'] + _noneAsEmptyList(extraVMarguments)
    scalaDacapoJar = mx.library('DACAPO_SCALA').get_path(True)
    _gate_java_benchmark(vmargs + ['-jar', scalaDacapoJar, name, '-n', str(iterations)], r'^===== DaCapo 0\.1\.0(-SNAPSHOT)? ([a-zA-Z0-9_]+) PASSED in ([0-9]+) msec =====')

def compiler_gate_runner(suites, unit_test_runs, bootstrap_tests, tasks, extraVMarguments=None):

    # Run unit tests in hosted mode
    for r in unit_test_runs:
        r.run(suites, tasks, ['-XX:-UseJVMCICompiler'] + _noneAsEmptyList(extraVMarguments))

    # Run microbench in hosted mode (only for testing the JMH setup)
    for r in [MicrobenchRun('Microbench', ['TestJMH'], tags=GraalTags.benchmarktest)]:
        r.run(tasks, ['-XX:-UseJVMCICompiler'] + _noneAsEmptyList(extraVMarguments))

    # Run ctw against rt.jar on hosted
    with Task('CTW:hosted', tasks, tags=GraalTags.ctw) as t:
        if t: ctw(['--ctwopts', 'Inline=false ExitVMOnException=true', '-esa', '-XX:-UseJVMCICompiler', '-Dgraal.CompileTheWorldMultiThreaded=true', '-Dgraal.InlineDuringParsing=false', '-Dgraal.CompileTheWorldVerbose=false', '-XX:ReservedCodeCacheSize=300m'], _noneAsEmptyList(extraVMarguments))

    # bootstrap tests
    for b in bootstrap_tests:
        b.run(tasks, extraVMarguments)

    # run selected DaCapo benchmarks
    dacapos = {
        'avrora':     1,
        'batik':      1,
        'fop':        8,
        'h2':         1,
        'jython':     2,
        'luindex':    1,
        'lusearch':   4,
        'pmd':        1,
        'sunflow':    2,
        'xalan':      1,
    }
    for name, iterations in sorted(dacapos.iteritems()):
        with Task('DaCapo:' + name, tasks, tags=GraalTags.benchmarktest) as t:
            if t: _gate_dacapo(name, iterations, _noneAsEmptyList(extraVMarguments) + ['-XX:+UseJVMCICompiler'])

    # run selected Scala DaCapo benchmarks
    scala_dacapos = {
        'actors':     1,
        'apparat':    1,
        'factorie':   1,
        'kiama':      4,
        'scalac':     1,
        'scaladoc':   1,
        'scalap':     1,
        'scalariform':1,
        'scalatest':  1,
        'scalaxb':    1,
        'tmt':        1
    }
    for name, iterations in sorted(scala_dacapos.iteritems()):
        with Task('ScalaDaCapo:' + name, tasks, tags=GraalTags.benchmarktest) as t:
            if t: _gate_scala_dacapo(name, iterations, _noneAsEmptyList(extraVMarguments) + ['-XX:+UseJVMCICompiler'])

    # ensure -Xbatch still works
    with Task('DaCapo_pmd:BatchMode', tasks, tags=GraalTags.test) as t:
        if t: _gate_dacapo('pmd', 1, _noneAsEmptyList(extraVMarguments) + ['-XX:+UseJVMCICompiler', '-Xbatch'])

    # ensure benchmark counters still work
    with Task('DaCapo_pmd:BenchmarkCounters', tasks, tags=GraalTags.test) as t:
        if t: _gate_dacapo('pmd', 1, _noneAsEmptyList(extraVMarguments) + ['-XX:+UseJVMCICompiler', '-Dgraal.LIRProfileMoves=true', '-Dgraal.GenericDynamicCounters=true', '-XX:JVMCICounterSize=10'])

    # ensure -Xcomp still works
    with Task('XCompMode:product', tasks, tags=GraalTags.test) as t:
        if t: run_vm(_noneAsEmptyList(extraVMarguments) + ['-XX:+UseJVMCICompiler', '-Xcomp', '-version'])

graal_unit_test_runs = [
    UnitTestRun('UnitTests', [], tags=GraalTags.test),
]

_registers = 'o0,o1,o2,o3,f8,f9,d32,d34' if mx.get_arch() == 'sparcv9' else 'rbx,r11,r10,r14,xmm3,xmm11,xmm14'

graal_bootstrap_tests = [
    BootstrapTest('BootstrapWithSystemAssertions', ['-esa', '-Dgraal.VerifyGraalGraphs=true', '-Dgraal.VerifyGraalGraphEdges=true', '-Dgraal.VerifyGraalPhasesSize=true'], tags=GraalTags.bootstraplite),
    BootstrapTest('BootstrapWithSystemAssertionsNoCoop', ['-esa', '-XX:-UseCompressedOops', '-Dgraal.ExitVMOnException=true'], tags=GraalTags.bootstrap),
    BootstrapTest('BootstrapWithGCVerification', ['-XX:+UnlockDiagnosticVMOptions', '-XX:+VerifyBeforeGC', '-XX:+VerifyAfterGC', '-Dgraal.ExitVMOnException=true'], tags=[GraalTags.bootstrap], suppress=['VerifyAfterGC:', 'VerifyBeforeGC:']),
    BootstrapTest('BootstrapWithG1GCVerification', ['-XX:+UnlockDiagnosticVMOptions', '-XX:-UseSerialGC', '-XX:+UseG1GC', '-XX:+VerifyBeforeGC', '-XX:+VerifyAfterGC', '-Dgraal.ExitVMOnException=true'], tags=[GraalTags.bootstrap], suppress=['VerifyAfterGC:', 'VerifyBeforeGC:']),
    BootstrapTest('BootstrapEconomyWithSystemAssertions', ['-esa', '-Dgraal.CompilerConfiguration=economy', '-Dgraal.ExitVMOnException=true'], tags=GraalTags.bootstrap),
    BootstrapTest('BootstrapWithExceptionEdges', ['-esa', '-Dgraal.StressInvokeWithExceptionNode=true', '-Dgraal.ExitVMOnException=true'], tags=GraalTags.bootstrap),
    BootstrapTest('BootstrapWithRegisterPressure', ['-esa', '-Dgraal.RegisterPressure=' + _registers, '-Dgraal.ExitVMOnException=true', '-Dgraal.LIRUnlockBackendRestart=true'], tags=GraalTags.bootstrap),
    BootstrapTest('BootstrapTraceRAWithRegisterPressure', ['-esa', '-Dgraal.TraceRA=true', '-Dgraal.RegisterPressure=' + _registers, '-Dgraal.ExitVMOnException=true', '-Dgraal.LIRUnlockBackendRestart=true'], tags=GraalTags.bootstrap),
    BootstrapTest('BootstrapWithImmutableCode', ['-esa', '-Dgraal.ImmutableCode=true', '-Dgraal.VerifyPhases=true', '-Dgraal.ExitVMOnException=true'], tags=GraalTags.bootstrap),
]

def _graal_gate_runner(args, tasks):
    compiler_gate_runner(['graal-core', 'truffle'], graal_unit_test_runs, graal_bootstrap_tests, tasks, args.extra_vm_argument)

mx_gate.add_gate_runner(_suite, _graal_gate_runner)
mx_gate.add_gate_argument('--extra-vm-argument', action='append', help='add extra vm argument to gate tasks if applicable (multiple occurrences allowed)')

def _unittest_vm_launcher(vmArgs, mainClass, mainClassArgs):
    run_vm(vmArgs + [mainClass] + mainClassArgs)

def _unittest_config_participant(config):
    vmArgs, mainClass, mainClassArgs = config
    cpIndex, cp = mx.find_classpath_arg(vmArgs)
    if cp:
        cp = _uniqify(cp.split(os.pathsep))
        if isJDK8:
            # Remove entries from class path that are in Graal or on the boot class path
            redundantClasspathEntries = set()
            for dist in [entry.dist() for entry in _jvmci_classpath]:
                redundantClasspathEntries.update((d.output_dir() for d in dist.archived_deps() if d.isJavaProject()))
                redundantClasspathEntries.add(dist.path)
            cp = os.pathsep.join([e for e in cp if e not in redundantClasspathEntries])
            vmArgs[cpIndex] = cp
        else:
            deployedModules = []
            redundantClasspathEntries = set()
            for dist in [entry.dist() for entry in _jvmci_classpath] + _bootclasspath_appends:
                deployedModule = as_java_module(dist, jdk)
                deployedModules.append(deployedModule)
                redundantClasspathEntries.update(mx.classpath(dist, preferProjects=False, jdk=jdk).split(os.pathsep))
                redundantClasspathEntries.update(mx.classpath(dist, preferProjects=True, jdk=jdk).split(os.pathsep))

            # Remove entries from the class path that are in the deployed modules
            cp = [classpathEntry for classpathEntry in cp if classpathEntry not in redundantClasspathEntries]
            vmArgs[cpIndex] = os.pathsep.join(cp)

            # Junit libraries are made into automatic modules so that they are visible to tests
            # patched into modules. These automatic modules must be declared to be read by
            # Graal which means they must also be made root modules (i.e., ``--add-modules``)
            # since ``--add-reads`` can only be applied to root modules.
            junitCp = [e.classpath_repr() for e in mx.classpath_entries(['JUNIT'])]
            junitModules = [_automatic_module_name(e) for e in junitCp]
            vmArgs.append('--module-path=' + os.pathsep.join(junitCp))
            vmArgs.append('--add-modules=' + ','.join(junitModules + [m.name for m in deployedModules]))
            for deployedModule in deployedModules:
                vmArgs.append('--add-reads=' + deployedModule.name + '=' + ','.join(junitModules))

            # Explicitly export concealed JVMCI packages required by Graal. Even though
            # normally exported via jdk.vm.ci.services.Services.exportJVMCITo(), the
            # Junit harness wants to access JVMCI classes (e.g., when loading classes
            # to find test methods) without going through that entry point.
            addedExports = {}
            for deployedModule in deployedModules:
                for concealingModule, packages in deployedModule.concealedRequires.iteritems():
                    if concealingModule == 'jdk.vm.ci':
                        for package in packages:
                            addedExports.setdefault(concealingModule + '/' + package, set()).add(deployedModule.name)

            patches = {}
            pathToProject = {p.output_dir() : p for p in mx.projects() if p.isJavaProject()}
            for classpathEntry in cp:
                # Export concealed packages used by the class path entry
                _add_exports_for_concealed_packages(classpathEntry, pathToProject, addedExports, 'ALL-UNNAMED', deployedModules)

                for deployedModule in deployedModules:
                    assert deployedModule.dist.path != classpathEntry, deployedModule.dist.path + ' should no longer be on the class path'
                    # Patch the class path entry into a module if it defines packages already defined by the module.
                    # Packages definitions cannot be split between modules.
                    classpathEntryPackages = frozenset(_defined_packages(classpathEntry))
                    if not classpathEntryPackages.isdisjoint(deployedModule.packages):
                        patches.setdefault(deployedModule.name, []).append(classpathEntry)
                        extraPackages = classpathEntryPackages - frozenset(deployedModule.exports.iterkeys())
                        if extraPackages:
                            # From http://openjdk.java.net/jeps/261:
                            # If a package found in a module definition on a patch path is not already exported
                            # by that module then it will, still, not be exported. It can be exported explicitly
                            # via either the reflection API or the --add-exports option.
                            for package in extraPackages:
                                addedExports.setdefault(deployedModule.name + '/' + package, set()).update(junitModules + ['ALL-UNNAMED'])

            for moduleName, cpEntries in patches.iteritems():
                vmArgs.append('--patch-module=' + moduleName + '=' + os.pathsep.join(cpEntries))

            vmArgs.extend(['--add-exports=' + export + '=' + ','.join(sorted(targets)) for export, targets in addedExports.iteritems()])

    if isJDK8:
        # Run the VM in a mode where application/test classes can
        # access JVMCI loaded classes.
        vmArgs.append('-XX:-UseJVMCIClassLoader')

    return (vmArgs, mainClass, mainClassArgs)

mx_unittest.add_config_participant(_unittest_config_participant)
mx_unittest.set_vm_launcher('JDK9 VM launcher', _unittest_vm_launcher, jdk)

def _uniqify(alist):
    """
    Processes given list to remove all duplicate entries, preserving only the first unique instance for each entry.

    :param list alist: the list to process
    :return: `alist` with all duplicates removed
    """
    seen = set()
    return [e for e in alist if e not in seen and seen.add(e) is None]

def _defined_packages(classpathEntry):
    """
    Gets the packages defined by `classpathEntry`.
    """
    packages = set()
    if os.path.isdir(classpathEntry):
        for root, _, filenames in os.walk(classpathEntry):
            if any(f.endswith('.class') for f in filenames):
                package = root[len(classpathEntry) + 1:].replace(os.sep, '.')
                packages.add(package)
    elif classpathEntry.endswith('.zip') or classpathEntry.endswith('.jar'):
        with zipfile.ZipFile(classpathEntry, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('.class') and '/' in name:
                    package = name[0:name.rfind('/')].replace('/', '.')
                    packages.add(package)
    return packages

def _automatic_module_name(modulejar):
    """
    Derives the name of an automatic module from an automatic module jar according to
    specification of java.lang.module.ModuleFinder.of(Path... entries).

    :param str modulejar: the path to a jar file treated as an automatic module
    :return: the name of the automatic module derived from `modulejar`
    """

    # Drop directory prefix and .jar (or .zip) suffix
    name = os.path.basename(modulejar)[0:-4]

    # Find first occurrence of -${NUMBER}. or -${NUMBER}$
    m = re.search(r'-(\d+(\.|$))', name)
    if m:
        name = name[0:m.start()]

    # Finally clean up the module name
    name = re.sub(r'[^A-Za-z0-9]', '.', name) # replace non-alphanumeric
    name = re.sub(r'(\.)(\1)+', '.', name) # collapse repeating dots
    name = re.sub(r'^\.', '', name) # drop leading dots
    return re.sub(r'\.$', '', name) # drop trailing dots

def _add_exports_for_concealed_packages(classpathEntry, pathToProject, exports, module, modulepath):
    """
    Adds exports for concealed packages imported by the project whose output directory matches `classpathEntry`.

    :param str classpathEntry: a class path entry
    :param dict pathToProject: map from an output directory to its defining `JavaProject`
    :param dict exports: map from a module/package specifier to the set of modules it must be exported to
    :param str module: the name of the module containing the classes in `classpathEntry`
    :param list modulepath: modules to be searched for concealed packages
    """
    project = pathToProject.get(classpathEntry, None)
    if project:
        concealed = project.get_concealed_imported_packages(jdk, modulepath)
        for concealingModule, packages in concealed.iteritems():
            for package in packages:
                exports.setdefault(concealingModule + '/' + package, set()).add(module)

def _extract_added_exports(args, addedExports):
    """
    Extracts ``--add-exports`` entries from `args` and updates `addedExports` based on their values.

    :param list args: command line arguments
    :param dict addedExports: map from a module/package specifier to the set of modules it must be exported to
    :return: the value of `args` minus all valid ``--add-exports`` entries
    """
    res = []
    for arg in args:
        if arg.startswith('--add-exports='):
            parts = arg[len('--add-exports='):].split('=', 1)
            if len(parts) == 2:
                export, targets = parts
                addedExports.setdefault(export, set()).update(targets.split(','))
            else:
                # Invalid format - let the VM deal with it
                res.append(arg)
        else:
            res.append(arg)
    return res

def _parseVmArgs(args, addDefaultArgs=True):
    args = mx.expand_project_in_args(args, insitu=False)

    argsPrefix = []
    jacocoArgs = mx_gate.get_jacoco_agent_args()
    if jacocoArgs:
        argsPrefix.extend(jacocoArgs)

    # add default graal.options.file
    options_file = join(mx.primary_suite().dir, 'graal.options')
    if exists(options_file):
        argsPrefix.append('-Dgraal.options.file=' + options_file)

    if '-Dgraal.PrintFlags=true' in args and '-Xcomp' not in args:
        mx.warn('Using -Dgraal.PrintFlags=true may have no effect without -Xcomp as Graal initialization is lazy')

    if isJDK8:
        argsPrefix.append('-Djvmci.class.path.append=' + os.pathsep.join((e.get_path() for e in _jvmci_classpath)))
        argsPrefix.append('-Xbootclasspath/a:' + os.pathsep.join([dep.classpath_repr() for dep in _bootclasspath_appends]))
    else:
        deployedDists = [entry.dist() for entry in _jvmci_classpath] + \
                        [e for e in _bootclasspath_appends if e.isJARDistribution()]
        deployedModules = [as_java_module(dist, jdk) for dist in deployedDists]

        # Set or update module path to include Graal and its dependencies as modules
        graalModulepath = []
        for deployedModule in deployedModules:
            graalModulepath.extend([jmd.jarpath for jmd in deployedModule.modulepath if jmd.jarpath])
            graalModulepath.append(deployedModule.jarpath)
        graalModulepath = _uniqify(graalModulepath)

        # Update added exports to include concealed JDK packages required by Graal
        addedExports = {}
        args = _extract_added_exports(args, addedExports)
        for deployedModule in deployedModules:
            for concealingModule, packages in deployedModule.concealedRequires.iteritems():
                # No need to explicitly export JVMCI - it's exported via reflection
                if concealingModule != 'jdk.vm.ci':
                    for package in packages:
                        addedExports.setdefault(concealingModule + '/' + package, set()).add(deployedModule.name)

        for export, targets in addedExports.iteritems():
            argsPrefix.append('--add-exports=' + export + '=' + ','.join(sorted(targets)))

        # Extend or set --module-path argument
        mpUpdated = False
        for mpIndex in range(len(args)):
            if args[mpIndex] == '--module-path':
                assert mpIndex + 1 < len(args), 'VM option ' + args[mpIndex] + ' requires an argument'
                args[mpIndex + 1] = os.pathsep.join(_uniqify(args[mpIndex + 1].split(os.pathsep) + graalModulepath))
                mpUpdated = True
                break
            elif args[mpIndex].startswith('--module-path='):
                mp = args[mpIndex][len('--module-path='):]
                args[mpIndex] = '--module-path=' + os.pathsep.join(_uniqify(mp.split(os.pathsep) + graalModulepath))
                mpUpdated = True
                break
        if not mpUpdated:
            argsPrefix.append('--module-path=' + os.pathsep.join(graalModulepath))

    # Set the JVMCI compiler to Graal
    argsPrefix.append('-Djvmci.Compiler=graal')

    if '-version' in args:
        ignoredArgs = args[args.index('-version') + 1:]
        if  len(ignoredArgs) > 0:
            mx.log("Warning: The following options will be ignored by the VM because they come after the '-version' argument: " + ' '.join(ignoredArgs))

    return jdk.processArgs(argsPrefix + args, addDefaultArgs=addDefaultArgs)

def _check_bootstrap_config(args):
    """
    Issues a warning if `args` denote -XX:+BootstrapJVMCI but -XX:-UseJVMCICompiler.
    """
    bootstrap = False
    useJVMCICompiler = False
    for arg in args:
        if arg == '-XX:+BootstrapJVMCI':
            bootstrap = True
        elif arg == '-XX:+UseJVMCICompiler':
            useJVMCICompiler = True
    if bootstrap and not useJVMCICompiler:
        mx.warn('-XX:+BootstrapJVMCI is ignored since -XX:+UseJVMCICompiler is not enabled')

def run_java(args, nonZeroIsFatal=True, out=None, err=None, cwd=None, timeout=None, env=None, addDefaultArgs=True):
    args = ['-XX:+UnlockExperimentalVMOptions', '-XX:+EnableJVMCI'] + _parseVmArgs(args, addDefaultArgs=addDefaultArgs)
    _check_bootstrap_config(args)
    cmd = get_vm_prefix() + [jdk.java] + ['-server'] + args
    return mx.run(cmd, nonZeroIsFatal=nonZeroIsFatal, out=out, err=err, cwd=cwd, env=env)

_JVMCI_JDK_TAG = 'jvmci'

class GraalJVMCI9JDKConfig(mx.JDKConfig):
    """
    A JDKConfig that configures Graal as the JVMCI compiler.
    """
    def __init__(self):
        mx.JDKConfig.__init__(self, jdk.home, tag=_JVMCI_JDK_TAG)

    def run_java(self, args, **kwArgs):
        return run_java(args, **kwArgs)

class GraalJDKFactory(mx.JDKFactory):
    def getJDKConfig(self):
        return GraalJVMCI9JDKConfig()

    def description(self):
        return "JVMCI JDK with Graal"

mx.addJDKFactory(_JVMCI_JDK_TAG, mx.JavaCompliance('9'), GraalJDKFactory())

def run_vm(args, nonZeroIsFatal=True, out=None, err=None, cwd=None, timeout=None, debugLevel=None, vmbuild=None):
    """run a Java program by executing the java executable in a JVMCI JDK"""
    return run_java(args, nonZeroIsFatal=nonZeroIsFatal, out=out, err=err, cwd=cwd, timeout=timeout)

class GraalArchiveParticipant:
    def __init__(self, dist, isTest=False):
        self.dist = dist
        self.isTest = isTest

    def __opened__(self, arc, srcArc, services):
        self.services = services
        self.arc = arc

    def __add__(self, arcname, contents):
        if arcname.startswith('META-INF/providers/'):
            if self.isTest:
                # The test distributions must not have their @ServiceProvider
                # generated providers converted to real services otherwise
                # bad things can happen such as InvocationPlugins being registered twice.
                pass
            else:
                provider = arcname[len('META-INF/providers/'):]
                for service in contents.strip().split(os.linesep):
                    assert service
                    self.services.setdefault(service, []).append(provider)
            return True
        elif arcname.endswith('_OptionDescriptors.class'):
            if self.isTest:
                mx.warn('@Option defined in test code will be ignored: ' + arcname)
            else:
                # Need to create service files for the providers of the
                # jdk.vm.ci.options.Options service created by
                # jdk.vm.ci.options.processor.OptionProcessor.
                provider = arcname[:-len('.class'):].replace('/', '.')
                self.services.setdefault('com.oracle.graal.options.OptionDescriptors', []).append(provider)
        return False

    def __addsrc__(self, arcname, contents):
        return False

    def __closing__(self):
        pass

mx.add_argument('--vmprefix', action='store', dest='vm_prefix', help='prefix for running the VM (e.g. "gdb --args")', metavar='<prefix>')
mx.add_argument('--gdb', action='store_const', const='gdb --args', dest='vm_prefix', help='alias for --vmprefix "gdb --args"')
mx.add_argument('--lldb', action='store_const', const='lldb --', dest='vm_prefix', help='alias for --vmprefix "lldb --"')

mx.update_commands(_suite, {
    'vm': [run_vm, '[-options] class [args...]'],
    'ctw': [ctw, '[-vmoptions|noinline|nocomplex|full]'],
})

def mx_post_parse_cmd_line(opts):
    mx.add_ide_envvar('JVMCI_VERSION_CHECK')
    for dist in _suite.dists:
        dist.set_archiveparticipant(GraalArchiveParticipant(dist, isTest=dist.name.endswith('_TEST')))
    add_bootclasspath_append(mx.distribution('truffle:TRUFFLE_API'))
    global _vm_prefix
    _vm_prefix = opts.vm_prefix

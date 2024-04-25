# Copyright 2024 - The Android Open Source Project

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import os
import typing as T
from pathlib import Path

from ... import build, mlog
from ...compilers import compilers
from ...dependencies.pkgconfig import PkgConfigDependency
from ...dependencies.platform import AppleFrameworks
from ...mesonlib import (
    File,
    FileOrString,
    MesonBugException,
    OrderedSet,
    ProgressBar,
    get_compiler_for_source,
)
from ..backends import Backend
from .bazel_rules import (
    BazelRule,
    BazelRuleLibrary,
    ExtractionRule,
    meson_target_as_bazel_label,
)
from .header_extractor import HeaderExtractor
from .path_resolver import PathResolver

if T.TYPE_CHECKING:
    from ..._typing import ImmutableListProtocol


def join_pairs(pairs_list):
    joined_pairs = []
    for i in range(0, len(pairs_list), 2):
        joined_pairs.append(" ".join(pairs_list[i : i + 2]))
    return joined_pairs


class TargetData:
    def __init__(self, target):
        self.target = target
        self.includes = OrderedSet(
            [z for x in target.get_include_dirs() for z in x.get_incdirs()]
        )
        self.deps = OrderedSet(
            meson_target_as_bazel_label(t) for t in target.get_dependencies()
        )
        self.hdrs = OrderedSet()
        self.defines = OrderedSet()
        self.warnings = OrderedSet()
        self.fopts = OrderedSet()
        self.linkopts = []
        self.compiled_sources = []


class ProcessResult(T.NamedTuple):
    """Represents a processed source file."""

    hdrs: OrderedSet
    deps: OrderedSet
    includes: OrderedSet
    defines: OrderedSet
    warnings: OrderedSet
    fopts: OrderedSet
    compiled_sources: list


class BuildTargetGenerator:

    supports = ["c", "cpp", "objc"]
    DEBUG_LOG = False

    def __init__(
        self,
        library: BazelRuleLibrary,
        backend: Backend,
        resolver: PathResolver,
        header_extractor: HeaderExtractor,
    ):
        self.library = library
        self.backend = backend
        self.resolver = resolver
        self.header_extractor = header_extractor

    def generate_inc_dir(
        self, compiler: "Compiler", d: str, basedir: str, is_system: bool
    ) -> T.Tuple["ImmutableListProtocol[str]", "ImmutableListProtocol[str]"]:
        # Avoid superfluous '/.' at the end of paths when d is '.'
        if d not in ("", "."):
            expdir = os.path.normpath(os.path.join(basedir, d))
        else:
            expdir = basedir
        srctreedir = os.path.normpath(os.path.join(self.backend.build_to_src, expdir))
        sargs = compiler.get_include_args(srctreedir, is_system)
        # There may be include dirs where a build directory has not been
        # created for some source dir. For example if someone does this:
        #
        # inc = include_directories('foo/bar/baz')
        #
        # But never subdir()s into the actual dir.
        if os.path.isdir(
            os.path.join(self.backend.environment.get_build_dir(), expdir)
        ):
            bargs = compiler.get_include_args(expdir, is_system)
        else:
            bargs = []
        return (sargs, bargs)

    def _generate_single_compile_target_args(
        self,
        target: build.BuildTarget,
        compiler: compilers.Compiler,
    ) -> ImmutableListProtocol[str]:
        # Add compiler args and include paths from several sources; defaults,
        # build options, external dependencies, etc.
        commands = self.backend.generate_basic_compiler_args(target, compiler, False)
        # Add custom target dirs as includes automatically, but before
        # target-specific include directories.
        if target.implicit_include_directories:
            commands += self.backend.get_custom_target_dir_include_args(
                target, compiler
            )
        # Add include dirs from the `include_directories:` kwarg on the target
        # and from `include_directories:` of internal deps of the target.
        #
        # Target include dirs should override internal deps include dirs.
        # This is handled in BuildTarget.process_kwargs()
        #
        # Include dirs from internal deps should override include dirs from
        # external deps and must maintain the order in which they are specified.
        # Hence, we must reverse the list so that the order is preserved.
        for i in reversed(target.get_include_dirs()):
            basedir = i.get_curdir()
            # We should iterate include dirs in reversed orders because
            # -Ipath will add to begin of array. And without reverse
            # flags will be added in reversed order.
            for d in reversed(i.get_incdirs()):
                # Add source subdir first so that the build subdir overrides it
                (compile_obj, includeargs) = self.generate_inc_dir(
                    compiler, d, basedir, i.is_system
                )
                commands += compile_obj
                commands += includeargs
            for d in i.get_extra_build_dirs():
                commands += compiler.get_include_args(d, i.is_system)
        # Add per-target compile args, f.ex, `c_args : ['-DFOO']`. We OrderedSet these
        # near the end since these are supposed to override everything else.
        commands += self.backend.escape_extra_args(
            target.get_extra_args(compiler.get_language())
        )

        # Add source dir and build dir. Project-specific and target-specific
        # include paths must override per-target compile args, include paths
        # from external dependencies, internal dependencies, and from
        # per-target `include_directories:`
        #
        # We prefer headers in the build dir over the source dir since, for
        # instance, the user might have an srcdir == builddir Autotools build
        # in their source tree. Many projects that are moving to Meson have
        # both Meson and Autotools in parallel as part of the transition.
        if target.implicit_include_directories:
            commands += self.backend.get_source_dir_include_args(target, compiler)
        if target.implicit_include_directories:
            commands += self.backend.get_build_dir_include_args(target, compiler)
        # Finally add the private dir for the target to the include path. This
        # must override everything else and must be the final path added.
        commands += compiler.get_include_args(
            self.backend.get_target_private_dir(target), False
        )
        return commands

    def _generate_single_compile_base_args(
        self, target: build.BuildTarget, compiler: compilers.Compiler
    ) -> compilers.CompilerArgs:
        base_proxy = target.get_options()
        # Create an empty commands list, and start adding arguments from
        # various sources in the order in which they must override each other
        commands = compiler.compiler_args()
        # Start with symbol visibility.
        commands += compiler.gnu_symbol_visibility_args(target.gnu_symbol_visibility)
        # Add compiler args for compiling this target derived from 'base' build
        # options passed on the command-line, in default_options, etc.
        # These have the lowest priority.
        commands += compilers.get_base_compile_args(base_proxy, compiler)
        return commands

    def generate_compile_commands_for_file(
        self,
        target: build.BuildTarget,
        src,
        header_deps=None,
        order_deps: T.Optional[T.List[FileOrString]] = None,
        extra_args: T.Optional[T.List[str]] = None,
    ) -> T.Tuple[compilers.Compiler, compilers.CompilerArgs]:
        """Compiles C/C++, ObjC/ObjC++"""
        header_deps = header_deps if header_deps is not None else []
        order_deps = order_deps if order_deps is not None else []

        if compilers.is_header(src):
            raise MesonBugException(f"Sources should not contain headers {src!r}")

        compiler = get_compiler_for_source(target.compilers.values(), src)
        commands = self._generate_single_compile_base_args(target, compiler)
        commands += self._generate_single_compile_target_args(target, compiler)
        commands = commands.compiler.compiler_args(commands)
        if extra_args is not None:
            commands.extend(extra_args)

        return compiler, commands

    def extract_defines(self, args: compilers.CompilerArgs) -> T.OrderedSet[str]:
        def escape_define_quote(arg: str) -> str:
            return arg.replace('"', '\\"')

        return OrderedSet(
            [escape_define_quote(item[2:]) for item in args if item.startswith("-D")]
        )

    def extract_warnings(self, args: compilers.CompilerArgs) -> T.OrderedSet[str]:
        return OrderedSet([item for item in args if item.startswith("-W")])

    def extract_fopts(self, args: compilers.CompilerArgs) -> T.OrderedSet[str]:
        return OrderedSet([item for item in args if item.startswith("-f")])

    def can_target_compile(self, target: build.BuildTarget, src) -> bool:
        for lang, compiler in target.compilers.items():
            if compiler.can_compile(src):
                return True

        return False

    def is_objc_file(self, file: File) -> bool:
        return file.endswith("m") or file.endswith(".mm")

    def apple_frameworks(self, target: build.BuildTarget):
        link_args = []
        for dep in target.external_deps:
            if not dep.is_found:
                continue

            if isinstance(dep, AppleFrameworks):
                # We are expecting "-framework", "...", "-framework", "..." pairs
                link_args += join_pairs(dep.link_args)

        return link_args

    def process_source(self, src, target):
        cc, args = self.generate_compile_commands_for_file(target, src)

        mlog.debug(f"Processing {target}:{src} -> {args}")
        hdrs = self.header_extractor.extract_headers_from_compiler_output(
            target, src, cc, args
        )
        deps = self.header_extractor.extract_external_dependencies(args)
        includes = self.header_extractor.extract_includes(args)
        defines = self.extract_defines(args)
        warnings = self.extract_warnings(args)
        fopts = self.extract_fopts(args)
        src_file = Path(os.path.join(src.subdir, src.fname)).as_posix()

        return ProcessResult(
            hdrs=hdrs,
            deps=deps,
            includes=includes,
            defines=defines,
            warnings=warnings,
            fopts=fopts,
            compiled_sources=[src_file],
        )

    def _generate_objc_library(self, target, objc_data):
        return self.library.register(
            BazelRule(
                "objc_library",
                {
                    "name": self._darwin_subname(target),
                    "srcs": OrderedSet(sorted([x for x in objc_data.compiled_sources])),
                    "hdrs": OrderedSet(sorted([x for x in objc_data.hdrs])),
                    "deps": OrderedSet(sorted(objc_data.deps)),
                    # TODO: Figure out what to do with these
                    # "copts": OrderedSet(objc_data.warnings).union(objc_data.fopts),
                    "defines": OrderedSet(objc_data.defines),
                    "linkopts": OrderedSet(objc_data.linkopts),
                    "alwayslink": hasattr(target, "alwayslink") and target.alwayslink,
                    "includes": OrderedSet(objc_data.includes),
                },
            )
        )

    def _generate_cc_binary(self, target, cc_data):
        return self.library.register(
            BazelRule(
                "cc_binary",
                {
                    "name": target.name,
                    "srcs": OrderedSet(
                        [x for x in cc_data.compiled_sources]
                        + [x for x in cc_data.hdrs]
                    ),
                    # TODO: Figure out what to do with these
                    # "copts": OrderedSet(cc_data.warnings).union(cc_data.fopts),
                    "linkopts": OrderedSet(cc_data.linkopts),
                    "deps": OrderedSet(cc_data.deps),
                    "defines": OrderedSet(cc_data.defines),
                    "includes": OrderedSet(cc_data.includes),
                },
            )
        )

    def _generate_shared_library(self, target, cc_data):
        return self.library.register(
            BazelRule(
                "cc_shared_library",
                {
                    "name": target.name,
                    "srcs": OrderedSet(
                        [x for x in cc_data.compiled_sources]
                        + [x for x in cc_data.hdrs]
                    ),
                    # TODO: Figure out what to do with these
                    # "copts": OrderedSet(cc_data.warnings).union(cc_data.fopts),
                    "linkopts": OrderedSet(cc_data.linkopts),
                    "deps": OrderedSet(cc_data.deps),
                    "defines": OrderedSet(cc_data.defines),
                    "includes": OrderedSet(cc_data.includes),
                },
            )
        )

    def _darwin_subname(self, target):
        return f"internal_{target.name}_darwin"

    def _generate_extraction_rule(self, target, cc_data, extended_deps):
        return self.library.register(
            ExtractionRule(
                {
                    "name": meson_target_as_bazel_label(target),
                    "alwayslink": hasattr(target, "alwayslink") and target.alwayslink,
                    "srcs": OrderedSet([x for x in cc_data.compiled_sources]),
                    "hdrs": OrderedSet([x for x in cc_data.hdrs]),
                    "deps": OrderedSet(cc_data.deps),
                    # "copts": OrderedSet(cc_data.warnings).union(cc_data.fopts),
                    "linkopts": OrderedSet(cc_data.linkopts),
                    "defines": OrderedSet(cc_data.defines),
                    "includes": OrderedSet(cc_data.includes),
                },
                extended_deps,
            )
        )

    def _process_results(self, results, target):
        """Processes the results of source file analysis.

        This function updates `cc_data` and `objc_data` (TargetData objects) based on
        the extracted information from source file analysis.

        Args:
            results: A list of ProcessResult objects containing extracted information.
            target: The Meson build target being processed.

        Returns:
            A tuple containing the updated `cc_data` and `objc_data` objects.
        """
        cc_data = TargetData(target)
        objc_data = TargetData(target)

        for result in results:
            data = (
                objc_data if self.is_objc_file(result.compiled_sources[0]) else cc_data
            )
            mlog.debug(f"Updating includes: {data.includes} -> {result.includes}")
            data.hdrs.update(result.hdrs)
            data.deps.update(result.deps)
            data.includes.update(result.includes)
            data.defines.update(result.defines)
            data.fopts.update(result.fopts)
            data.warnings.update(result.warnings)
            data.compiled_sources.extend(result.compiled_sources)

        # Next we are going to remove unused includes:
        cc_data.includes = OrderedSet(
            [
                x
                for x in cc_data.includes
                if x.startswith("platform")
                or x == "."
                or any(hdr.startswith(x) for hdr in cc_data.hdrs)
            ]
        )
        objc_data.includes = OrderedSet(
            [
                x
                for x in objc_data.includes
                if x.startswith("platform")
                or x == "."
                or any(hdr.startswith(x) for hdr in objc_data.hdrs)
            ]
        )

        return cc_data, objc_data

    def _get_extended_deps(self, target, cc_data, objc_data):
        """Extracts extended dependencies from extracted objects.

        Args:
            target: The Meson build target being processed.
            cc_data: TargetData object for C/C++ sources.
            objc_data: TargetData object for Objective-C sources.

        Returns:
            A dictionary of extended dependencies, mapping target names to source lists.
        """

        extended_deps = {}
        for obj in target.objects:
            if isinstance(obj, build.ExtractedObjects):
                mlog.debug(f"Extended {target} -E-> {obj.target.name}")

                cpp_sources = [
                    self.resolver.find(z).as_posix()
                    for z in obj.srclist
                    if not z.is_built and not self.is_objc_file(z)
                ]
                objc_sources = [
                    self.resolver.find(z).as_posix()
                    for z in obj.srclist
                    if not z.is_built and self.is_objc_file(z)
                ]

                if cpp_sources:
                    extended_deps[meson_target_as_bazel_label(obj.target)] = cpp_sources
                    mlog.debug(
                        f"Extended deps: {target.name} ==> {obj.target.name}:"
                        f" {cpp_sources}"
                    )

                if objc_sources:
                    tgt = self._darwin_subname(obj.target)
                    extended_deps[tgt] = objc_sources
                    mlog.debug(
                        f"Extended deps: {target.name} ==> {tgt}: {objc_sources}"
                    )

        return extended_deps

    def generate(self, target: build.StaticLibrary) -> BazelRule:
        if self.library.is_registered(target.name):
            return self.library.get(target.name)

        if target.name == "qemu-aarch64-softmmu":
            pass

        if not any([c in target.compilers for c in self.supports]):
            raise MesonBugException(
                f"No compiler that supports {self.supports} for {target.name}"
            )

        sources: T.List[File] = [s for s in target.get_sources() if not s.is_built]

        generated: T.List[File] = [
            s for s in self.backend.get_target_generated_sources(target)
        ]

        mlog.debug(f"Generated sources: {generated}")
        # Filter out things we cannot process
        to_process = []
        for src in sources + generated:
            if compilers.is_header(src) or not self.can_target_compile(target, src):
                if self.DEBUG_LOG:
                    mlog.debug(f"Not compiling: {src}")
                continue
            to_process.append(src)

        mlog.debug(f"generating library: {target.name}.")
        results = []
        for src in ProgressBar(to_process, desc=f"Analyzing {target.name}"):
            results.append(self.process_source(src, target))

        # Process the source results to generate the hdrs, include and copts tags
        cc_data, objc_data = self._process_results(results, target)
        cc_data.linkopts += self.apple_frameworks(target)
        objc_data.linkopts += self.apple_frameworks(target)
        cc_data.deps.update([meson_target_as_bazel_label(x) for x in target.get_dependencies()])
        objc_data.deps.update(
            [meson_target_as_bazel_label(x) for x in target.get_dependencies()]
        )

        extended_deps = self._get_extended_deps(target, cc_data, objc_data)
        # Bazel will not be able to handle objc in a cc_ rule, instead we
        # will need to filter out the objc files, create an intermediate target
        # objc_ target and take a dependency on that.
        if objc_data.compiled_sources:
            objc_data.linkopts += self.apple_frameworks(target)
            objc_data.deps.update(
                [meson_target_as_bazel_label(x) for x in target.get_dependencies()]
            )
            objc_lib = self._generate_objc_library(target, objc_data)

            if not cc_data.compiled_sources:
                return objc_lib

            # If there is a cc_lib, then we should register it.
            cc_data.deps.add(objc_lib.name)

        if isinstance(target, build.Executable):
            m = self.backend.environment.machines[target.for_machine]
            # Make sure that windows uses the right subsystem if defined
            if m.is_windows() or m.is_cygwin():
                linker, _ = target.get_clink_dynamic_linker_and_stdlibs()
                cc_data.linkopts += linker.get_win_subsystem_args(target.win_subsystem)
            return self._generate_cc_binary(target, cc_data)

        if isinstance(target, build.SharedLibrary):
            return self._generate_shared_library(target, cc_data)

        if cc_data.compiled_sources:
            return self._generate_extraction_rule(target, cc_data, extended_deps)

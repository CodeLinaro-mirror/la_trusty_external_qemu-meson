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

import subprocess
import typing as T
from functools import lru_cache
from pathlib import Path

from ... import build, compilers, mlog
from ...mesonlib import MesonBugException, OrderedSet
from .path_resolver import PathResolver


class HeaderExtractor:
    """Utility class for extracting header file information from compiler output.

    Provides methods to parse compiler arguments, identify external dependencies,
    and extract header file paths suitable for Bazel, handling Meson-specific
    cases and resolving relative paths.
    """

    DEBUG_LOG = False

    def __init__(
        self,
        shadow_build_dir: Path,
        resolver: PathResolver,
    ):
        self.shadow_build_dir = shadow_build_dir
        if not Path(shadow_build_dir).exists():
            mlog.warning("No shadow dir present, falling back to build dir")
            self.shadow_build_dir = resolver.build_dir

        self.resolver = resolver
        self.source_dir = resolver.source_dir
        self.build_dir = resolver.build_dir
        self.include_dir_to_external_dependency = {}
        self.build_prefix = resolver.build_prefix

    def _parse_include_args(self, cc: compilers.CompilerArgs) -> OrderedSet[Path]:
        """Parses compiler arguments and extracts all include directories.

        Args:
            cc: A list of compiler arguments (CompilerArgs).

        Returns:
             A set of all extracted include directories as Path objects,
             including those specified with `-I`, `-iquote`, and `-isystem`
             directives.
        """
        include_dirs = OrderedSet()
        previous_element = None

        for current_element in cc:
            if previous_element in ["-iquote", "-isystem"]:
                include_dirs.add(
                    self.resolver.resolve_from_build(Path(current_element))
                )
            if current_element.startswith("-I"):
                include_dirs.add(
                    self.resolver.resolve_from_build(Path(current_element[2:]))
                )
            previous_element = current_element

        if self.DEBUG_LOG:
            mlog.debug(f"Found includes: {include_dirs}")
        return include_dirs

    def _external_include_directories(
        self, include_dirs: OrderedSet[Path]
    ) -> OrderedSet[Path]:
        """Filters include directories to identify those external to the project.

        Args:
            include_dirs: A set of include directory paths (Path objects).

        Returns:
            A set of Path objects representing include directories that are
            not located within the build directory (`self.build_dir`) or the
            source directory (`self.source_dir`), indicating they are likely
            external dependencies.
        """
        return OrderedSet([x for x in include_dirs if x.is_absolute()])

    def _extract_dependencies_from_compiler_output(
        self, dep_contents: T.List[str]
    ) -> T.List[str]:
        """Extracts dependency file names from compiler output.

        Parses the output of dependency generation options (e.g., GCC's `-M` or
        Clang's `-MD`) to extract referenced file names. It handles line
        continuations and ignores target object files.

        Args:
            dep_contents: A list of strings representing the lines of compiler
                dependency output.

        Returns:
            A list of extracted header file names (strings).
        """
        header_files = []
        if self.DEBUG_LOG:
            mlog.debug("Parsing dep file")
        skip_next = False
        for line in dep_contents:
            if self.DEBUG_LOG:
                mlog.debug(f"L: {line}")
            files = line.rstrip("\\").split()  # Remove trailing '\' and split
            for f in files:
                if skip_next:
                    skip_next = False
                    continue
                if f.endswith(":"):
                    skip_next = True
                    continue
                if self.DEBUG_LOG:
                    mlog.debug(f"     {f}")
                header_files.append(f)
        return header_files

    def _extract_bazel_headers_from_dep(
        self, dep_contents: [str], sys_headers: [str]
    ) -> [Path]:
        """Extracts header file paths for Bazel from compiler dependency output.

        Parses GCC/Clang dependency output, resolving paths relative to the build or
        source directory. Handles Meson-specific cases for generated files. Filters
        out external dependencies.

        Args:
            dep_contents: A list of strings representing the lines of compiler
                dependency output.

        Returns:
            A list of header file paths (Path objects) suitable for Bazel.
        """
        header_files = self._extract_dependencies_from_compiler_output(dep_contents)
        bazel_headers = OrderedSet()

        for f in header_files:
            # Skip all system headers.
            if any([f.startswith(s) for s in sys_headers]):
                if self.DEBUG_LOG:
                    mlog.debug(f"Skipping {f}")
                continue

            try:
                path = self.resolver.find(f)
                if not path.is_absolute():
                    if self.DEBUG_LOG:
                        mlog.debug(f"Header: {f} resolved to --> {path}")
                    bazel_headers.add(path.as_posix())
                else:
                    if self.DEBUG_LOG:
                        mlog.debug(f"Header: Ignoring: {f}")
            except FileNotFoundError:
                if self.DEBUG_LOG:
                    mlog.debug(f"Header: {f} not found!")

        return bazel_headers

    def extract_external_dependencies(self, cc: compilers.CompilerArgs) -> T.Set[str]:
        """Extracts external dependencies from compiler arguments.

        Args:
            cc: A list of compiler arguments (CompilerArgs).

        Returns:
            A set of external dependency names (e.g., "glib", "pcre2").
        """

        include_dirs = self._parse_include_args(cc)  # Get include directories
        external_dirs = self._external_include_directories(
            include_dirs
        )  # Filter externals

        dependencies = set()
        for d in external_dirs:
            dep_names = self.include_dir_to_external_dependency.get(
                d
            )  # Lookup dependency
            if dep_names:
                for dep_name in dep_names:
                    dependencies.add(dep_name)
            else:
                mlog.warning(
                    f"No dependency found for include directory: {d}", once=True
                )

        return dependencies

    def extract_includes(self, cc: compilers.CompilerArgs) -> OrderedSet[str]:
        include_dirs = self._parse_include_args(cc)  # Get include directories
        external_dirs = self._external_include_directories(
            include_dirs
        )  # Filter externals

        # Note that all the directories are:
        # 1. Relative to the build_dir
        # 2. Absolute paths
        include_dirs: OrderedSet[Path] = include_dirs.difference(external_dirs)

        # Make our build prefix available
        include_dirs.add(self.build_prefix)

        return [x.as_posix() for x in include_dirs]

    def parse_gcc_include_paths(self, output):
        """
        Parses the output of `gcc -E -Wp,-v` to extract include paths.

        Args:
            output (str): The output string from the command.

        Returns:
            list: A list of include paths, or an empty list if none found.
        """

        lines = output.splitlines()
        start_index = -1
        end_index = -1

        # Find the start and end markers for the include paths section
        for i, line in enumerate(lines):
            if line.startswith("#include <...> search starts here:"):
                start_index = i + 1
            elif line.startswith("End of search list."):
                end_index = i
                break

        if start_index == -1 or end_index == -1:
            return []  # No include paths found

        # Extract and clean the paths
        include_paths = []
        for line in lines[start_index:end_index]:
            path = line.strip()
            if path:
                include_paths.append(path)

        if self.DEBUG_LOG:
            mlog.debug(f"Sys_headers: {include_paths}")
        return include_paths

    @lru_cache(maxsize=None)
    def sys_headers(self, cc: compilers.Compiler):
        try:
            cmd = cc.get_exelist() + ["-E", "-Wp,-v", "-"]
            if self.DEBUG_LOG:
                mlog.debug(
                    f"Header: extracting sys_headers with compiler (cd {self.shadow_build_dir} && {' '.join(cmd)})"
                )

            res = subprocess.run(
                [str(x) for x in cmd],
                cwd=self.shadow_build_dir,
                input="",
                encoding="utf-8",
                capture_output=True,
                text=True,
                check=True,
            )

            return sorted(self.parse_gcc_include_paths(res.stderr))

        except subprocess.CalledProcessError:
            raise MesonBugException(
                f"Failed to extract headers with {' '.join(cmd)} in {self.shadow_build_dir}, which is part of {target}"
            )

    def extract_headers_from_compiler_output(
        self,
        target: build.BuildTarget,
        srcfile: build.File,
        cc: compilers.Compiler,
        args: compilers.CompilerArgs,
    ) -> T.List[Path]:
        """Extracts header file paths from compiler dependency output.

        Invokes the compiler with the `-M` flag to generate dependency information
        for the given source file. Parses the output to extract header file paths
        suitable for Bazel, handling Meson-specific cases and filtering out external
        dependencies.

        Args:
            target: The build target associated with the source file.
            srcfile: The source file to extract headers for.
            cc: The compiler to use.
            args: The compiler arguments to use.

        Returns:
            A list of header file paths (Path objects) suitable for Bazel.

        Raises:
            MesonBugException: If the compiler invocation fails or the output
                cannot be parsed.
        """

        args.append("-M")
        cmd = (
            cc.get_exelist()
            + [x for x in args]
            + [
                srcfile.absolute_path(
                    self.resolver.source_dir,
                    self.shadow_build_dir,
                )
            ]
        )
        try:
            sys_headers = self.sys_headers(cc)

            if self.DEBUG_LOG:
                mlog.debug(
                    f"Invoking compiler (cd {self.shadow_build_dir} && {' '.join(cmd)})"
                )
            out = subprocess.check_output(
                cmd,
                cwd=self.shadow_build_dir,
                encoding="utf-8",
            )
            return self._extract_bazel_headers_from_dep(out.splitlines(), sys_headers)

        except subprocess.CalledProcessError:
            raise MesonBugException(
                f"Failed to extract headers with {' '.join(cmd)} in {self.shadow_build_dir}, which is part of {target}"
            )

    def build_external_dependency_map(self, targets: T.List[build.Target]):
        """Constructs a mapping between external include directories and their dependencies.

        Iterates through build targets, extracts external dependencies, and maps their
        include directories to the corresponding dependency names. This mapping helps
        determine which target needs to be included when using a header from a given path.

        Args:
            targets: A list of build targets (build.Target objects).
        """
        external_deps = set()
        for t in targets:
            if isinstance(t, build.BuildTarget):
                external_deps.update([x for x in t.get_external_deps()])

        # Setup the mappings from our external dependencies based on includes.
        # This allows us to figure out which header requires which dependency
        for dep in external_deps:
            if hasattr(dep, "compile_args"):
                cc = dep.compile_args
                include_dirs = self._parse_include_args(cc)
                external_dirs = self._external_include_directories(include_dirs)
                for i in external_dirs:
                    # TODO a pkgconfigdependency can have transitive dependencies
                    # For example glib-2 depends on zlib
                    # We should sort these out here, so we do not add glib if you only
                    # need zlib for example.
                    if i not in self.include_dir_to_external_dependency:
                        self.include_dir_to_external_dependency[i] = OrderedSet()
                    self.include_dir_to_external_dependency[i].add(f"{dep.name}")

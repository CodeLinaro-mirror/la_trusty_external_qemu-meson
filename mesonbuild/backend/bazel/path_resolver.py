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

import typing as T
from functools import lru_cache
from pathlib import Path

from ... import mlog
from ...build import File, FileMaybeInTargetPrivateDir


class PathResolver:
    """Resolves paths within a Meson build environment.

    Handles paths from source, build, and shadow directories, taking into account
    symlinks and relative paths. Provides methods to normalize, resolve, and
    simplify paths for use in Bazel build rules.

    Args:
        source_dir (Path): Path to the source directory.
        shadow_dir (Path): Path to the shadow build directory.
        build_dir (Path): Path to the main build directory.
        prefix (str): Prefix to use for build artifacts.
    """
    DEBUG_LOG = False

    def __init__(
        self, source_dir: Path, shadow_dir: Path, build_dir: Path, prefix: str
    ):
        self.source_dir = source_dir
        self.shadow_dir = shadow_dir
        self.build_dir = build_dir
        self.build_prefix = prefix
        self.symlink_map = self.build_symlinks_map(self.source_dir)

    def build_symlinks_map(self, root_dir):
        """Recursively finds symlinks and creates a map from destination to link.

        Args:
            root_dir (Path): The path to the root directory to search.

        Returns:
            dict: A dictionary mapping symlink destinations to their paths.
        """
        symlink_map = {}

        for path in root_dir.rglob("*"):
            if path.is_symlink():
                destination = path.resolve()
                symlink_map[destination] = path

        return symlink_map

    @lru_cache(maxsize=None)
    def resolve_symlink_path(self, path):
        """Resolves a path (including subpaths) through symlinks, if applicable.

        Args:
            path (str or Path): The path to resolve.

        Returns:
            Path: The resolved path, or the original path if no symlinks are involved.
        """

        path = Path(path).resolve()  # Resolve initial path to handle absolute/relative

        parts = path.parts  # Split path into components
        resolved_parts = []

        for i, part in enumerate(parts):
            current_path = Path(*parts[: i + 1])  # Reconstruct path up to this part
            if current_path in self.symlink_map:
                resolved_parts = (
                    self.symlink_map[current_path].parts + parts[i + 1 :]
                )  # Replace with resolved symlink
                break  # Stop after resolving the first symlink in the path

        if resolved_parts:
            return Path(*resolved_parts)

        return path

    def simplify_path(self, path: T.Union[Path, str]) -> Path:
        """
        Simplifies a Unix-style path by handling ".." components.

        Args:
            path: The input path string.

        Returns:
            The simplified path string.
        """
        if not path:
            return Path()

        stack = []
        parts = Path(path).as_posix().split("/")

        if self.DEBUG_LOG: mlog.debug(f"Simplifying {path} -> {parts}")

        for part in parts:
            if part == "." or not part:  # Ignore "." and empty parts
                continue
            elif part == "..":
                if stack:  # Pop only if stack is not empty
                    stack.pop()
            else:
                stack.append(part)
        simple = "/" + "/".join(stack)

        if self.DEBUG_LOG: mlog.debug(f"Simple: {simple}")
        return Path(simple)

    def normalize_meson_path(
        self, meson_file: T.Union[str | File | Path | FileMaybeInTargetPrivateDir]
    ) -> Path:
        """Normalizes a Meson file path, handling different input types.

        Resolves the path to an absolute path within the source or build directory,
        depending on the file's origin.

        Args:
            meson_file: The Meson file path (str, File, Path, or FileMaybeInTargetPrivateDir).

        Returns:
            Path: The normalized absolute path.

        Raises:
            ValueError: If the input type is not recognized.
        """
        if isinstance(meson_file, str):
            return Path(meson_file)

        if isinstance(meson_file, File):
            abs_path = Path(meson_file.absolute_path(self.source_dir, self.build_dir))
            if meson_file.is_built:
                return self.resolve_from_build(abs_path)

            return self.resolve_from_source(abs_path)

        if isinstance(meson_file, Path):
            return meson_file

        if isinstance(meson_file, FileMaybeInTargetPrivateDir):
            return Path(meson_file.absolute_path(self.source_dir, self.build_dir))

        raise ValueError(f"Unknown file {meson_file} of type: {type(meson_file)}")

    @lru_cache(maxsize=None)
    def find(self, filename: str, symlinked: False = False) -> T.Union[Path | None]:
        """Finds a relative path to the file in either the shadow build or source tree.
        The file that is returned can be directly used in a bazel rule. It will
        point either to an "existing" file or a file that will be generated during
        the bazel build.

        This expects the shadow build directory to contain all the generated sources,
        required to build the target that is being processed.

        Resolves symlinks if necessary and returns a path suitable for Bazel.

        Args:
            filename (str): The filename to find.
            symlinked (bool, optional): Whether to resolve symlinks. Defaults to False.

        Returns:
            Path: The relative path to the file, or None if not found.

        Raises:
            FileNotFoundError: If the absolute path does not exist.
        """
        p = self.normalize_meson_path(filename)
        if p.is_absolute():
            if self.DEBUG_LOG: mlog.debug(f"Finding {filename}, normalized to abs: {p} exists: {p.exists()}")
            if not p.exists():
                raise FileNotFoundError(f"{p} does not exist.")

            # It is already an absolute path
            # and points to something that exists
            # it can be:
            # 1. In the source
            # 2. In our meson configuration directory
            # 3. In the shadow_build dir --> it will be generated
            resolved = self.resolve_symlink_path(p) if symlinked else p.resolve()
            if self.DEBUG_LOG: mlog.debug(f"{p} --> resolved {resolved} ({self.source_dir}, {self.shadow_dir}, {self.build_dir})")

            # Okay, we now have an absolute path that exists.
            # Let's see if we are case 1
            if resolved.is_relative_to(self.source_dir):
                if self.DEBUG_LOG: mlog.debug(f"!! {resolved}.is_relative_to({self.source_dir}) (self.source_dir)")
                return resolved.relative_to(self.source_dir)
            if resolved.is_relative_to(self.shadow_dir):
                if self.DEBUG_LOG: mlog.debug(f"!! {resolved}.is_relative_to({self.shadow_dir}) (self.shadow_dir)")
                relative = resolved.relative_to(self.shadow_dir)

                # Case 2? Does it exist in our build dir?
                if Path.joinpath(self.build_dir, relative).exists():
                    if self.DEBUG_LOG: mlog.debug(f"!! Path.joinpath({self.build_dir}, {relative}).exists():")
                    return self.build_prefix / relative

                # This will be generated.
                if self.DEBUG_LOG: mlog.debug(f"!! Generated {relative}")
                return relative

            if resolved.is_relative_to(self.build_dir):
                if self.DEBUG_LOG: mlog.debug(f"!! {resolved}.is_relative_to({self.build_dir}) (self.build_dir):")
                relative = resolved.relative_to(self.shadow_dir)
                return self.build_prefix / relative

            # okay, you do not exist in source or shadow. not good!
            if not symlinked:
                # Let's true to resolve it through a symlink..
                return self.find(resolved, True)
            raise FileNotFoundError(f"Unable to resolve {filename} last attempt was ({resolved})")

        # Ok we have a relative path.. It could exist in our source dir:
        if Path.joinpath(self.source_dir, p).exists():
            simplified = self.resolve_symlink_path(Path.joinpath(self.source_dir, p))
            if simplified.is_relative_to(self.source_dir):
                if self.DEBUG_LOG: mlog.debug(f"!! relative path to source {simplified.relative_to(self.source_dir)}")
                # Simplify the path..
                return simplified.relative_to(self.source_dir)


        # Let's resolve it from the build dir and see where we end up
        resolved_from_shadow = self.resolve_symlink_path(
            Path.joinpath(self.shadow_dir, p)
        )
        assert resolved_from_shadow.is_absolute()
        try:
            if self.DEBUG_LOG: mlog.debug("Trying resoving from shadow dir")
            return self.find(resolved_from_shadow, symlinked)
        except FileNotFoundError:
            if not symlinked:
                if self.DEBUG_LOG: mlog.debug("Trying to reverse resolve symlinks")
                return self.find(resolved_from_shadow, True)
            else:
                raise


    @lru_cache(maxsize=None)
    def resolve(self, path: Path) -> Path:
        """Resolves a path relative to the source or build directory.

        Handles symlinks and absolute/relative paths.

        Note: You likely want to use find, as that is more comprehensive.

        Args:
            path (Path): The path to resolve.

        Returns:
            Path: The resolved path relative to the base directory, or the original path
                if it cannot be resolved.
        """
        path = path.resolve()
        resolved_path = self.resolve_symlink_path(path)

        for base_dir in (self.source_dir, self.build_dir):
            if path.is_relative_to(base_dir):
                return path.relative_to(base_dir)
            if resolved_path.is_relative_to(base_dir):
                return resolved_path.relative_to(base_dir)
        return path

    def resolve_from_build(self, orig: Path) -> Path:
        """Resolves a path starting from the Meson build directory.

        Handles symlinks and attempts to find the path relative to the build or source
        directory.

        Args:
            path (Path): The path to resolve.

        Returns:
            Path: The resolved path relative to the base directory, or the original path
                if it cannot be resolved.
        """
        path = (self.build_dir / orig).resolve()
        resolved_path = self.resolve_symlink_path(path)
        if self.DEBUG_LOG: mlog.debug(f"Resolving from build: {orig} -> {path}, {resolved_path}")

        # The resolved path is now an absolute path,
        # possible through a symlink
        # This path is now an absolute path, which can be
        # one of 3:
        # 1. It is under the build dir
        # 2. It is under the source dir
        # 3. It is neither
        if resolved_path.is_relative_to(self.build_dir):
            if self.DEBUG_LOG: mlog.debug(f"{orig} is relative to build dir")
            return resolved_path.relative_to(self.build_dir)
        if resolved_path.is_relative_to(self.source_dir):
            if self.DEBUG_LOG: mlog.debug(f"{orig} is relative to source dir")
            return resolved_path.relative_to(self.source_dir)

        if self.DEBUG_LOG: mlog.debug(f"{orig} is not under source or build.")
        return resolved_path

    def resolve_from_source(self, orig: Path) -> Path:
        """Resolves a path starting from the Meson source directory.

        Handles symlinks and attempts to find the path relative to the source directory.

        Args:
            orig (Path): The path to resolve.

        Returns:
            Path: The resolved path relative to the source directory, or the original path
                if it cannot be resolved.
        """
        # Simplify symlink if present
        path = (self.source_dir / orig).resolve()
        resolved_path = self.resolve_symlink_path(path)
        if self.DEBUG_LOG: mlog.debug(f"Resolving from source: {orig} -> {path}, {resolved_path}")

        # Try resolving relative path from the source directory
        if resolved_path.is_relative_to(self.source_dir):
            if self.DEBUG_LOG: mlog.debug(f"{orig} is relative to source dir through symlinks")
            return resolved_path.relative_to(self.source_dir)
        if path.is_relative_to(self.source_dir):
            if self.DEBUG_LOG: mlog.debug(f"{orig} is relative to source dir ")
            return path.relative_to(self.source_dir)

        if self.DEBUG_LOG: mlog.debug("Unable to resolve path from source")
        return path

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
import platform
import subprocess
import typing as T
from functools import lru_cache
from pathlib import Path

from ... import build, mlog, programs
from ...mesonlib import (
    File,
    MesonBugException,
    OrderedSet,
    get_filenames_templates_dict,
    substitute_values,
)
from .bazel_rules import BazelRule, BazelRuleLibrary, as_bazel_label
from .path_resolver import PathResolver


@lru_cache
def is_python_exe(prog):
    if isinstance(prog, File):
        return "python" in prog.fname and "Python" in subprocess.check_output(
            [prog.fname, "--version"], encoding="utf-8"
        )
    return (
        isinstance(prog, str)
        and "python" in prog
        and "Python" in subprocess.check_output([prog, "--version"], encoding="utf-8")
    )


def is_resource_compiler(prog) -> bool:
    if "rc" not in prog:
        return False

    info = subprocess.check_output([prog, "-h"], encoding="utf-8")
    return (
        "Microsoft (R) Windows (R) Resource Compiler" in info
        or "LLVM Resource Converter" in info
    )


def get_bazel_target_from_script(file_path: str) -> T.Optional[str]:
    """Checks if a file is a bazel wrapper script and returns the bazel target."""
    try:
        with open(file_path, "rb") as f:
            # This prevents reading the entire file into memory if it's a large
            # binary with no newline. 256 bytes is more than enough for
            # a shebang or "@echo off" line.
            first_line = f.readline(256).strip()

            is_script = False
            if platform.system() == "Windows":
                if first_line == b"@echo off":
                    is_script = True
            else:
                if first_line.startswith(b"#!/"):
                    is_script = True

            if not is_script:
                return None

            # Reset and read as text to find the marker
            with open(file_path, "r", errors="ignore") as tf:
                for _ in range(10):
                    line = tf.readline()
                    if not line:
                        break
                    # Note the marker will be either # Bazel:, or rem Bazel: on windows
                    marker = " Bazel:"
                    if marker in line:
                        start_index = line.find(marker) + len(marker)
                        return line[start_index:].strip()
    except (IOError, OSError):
        return None
    return None


class CustomTargetGenerator:
    DEBUG_LOG = False

    def __init__(self, library: BazelRuleLibrary, backend, resolver: PathResolver):
        self.library = library
        self.backend = backend
        self.resolver = resolver

    def get_executable(self, target: build.CustomTarget, program: [str | File]) -> str:
        # It could be that we need to do input substitution
        if program == "@INPUT@":
            inputs = [
                self.resolver.resolve_from_build(Path(x)).as_posix()
                for x in self.backend.get_custom_target_sources(target)
            ]
            values = get_filenames_templates_dict(inputs, [])
            prog = substitute_values([program], values)[0]
            return Path(prog).with_suffix("").name
        if isinstance(program, File):
            program = program.relative_name()
        return Path(program).with_suffix("").name

    def create_py_binary(self, target: build.CustomTarget):
        """Creates a py_binary rule if needed for this target."""
        file_deps: T.List[File] = []

        # Make sure we don't accidentally override the existing cmds
        cmds = target.command.copy()

        # Two cases, the custom command explicitly calls the
        # python interpreter, so the actual entrypoint is cmd[1]
        if is_python_exe(cmds[0]):
            prog = self.get_executable(target, cmds[1])
            py = cmds[1]
        else:
            prog = self.get_executable(target, cmds[0])
            py = cmds[0]

        if py not in target.depend_files:
            file_deps.append(self.resolver.find(py).as_posix())

        # Binary has been created already.
        if self.library.is_registered(prog):
            return self.library.get(prog)

        file_deps += [f for f in target.depend_files if not is_python_exe(f)]
        inputs = [
            self.resolver.resolve_from_build(x).as_posix()
            for x in self.backend.get_custom_target_sources(target)
        ]

        values = get_filenames_templates_dict(inputs, [])
        file_deps = substitute_values(file_deps, values)

        # file_deps contains all the file dependencies
        # We need to split them into python and data files
        srcs = OrderedSet(
            sorted(
                self.resolver.find(x).as_posix() for x in file_deps if x.endswith(".py")
            )
        )
        data = OrderedSet(
            sorted(
                self.resolver.find(x).as_posix()
                for x in file_deps
                if not x.endswith(".py")
            )
        )

        return self.library.register(
            BazelRule(
                "py_binary",
                {
                    "name": prog,
                    "data": data,
                    "srcs": srcs,
                },
            )
        )

    def create_resource_rule(self, target):
        mlog.warning(
            "Creating windows_resources, this will require windows_resources.bzl"
        )
        return self.library.register(
            BazelRule(
                "windows_resources",
                {
                    "name": target.name,
                    "rc_files": [
                        self.resolver.find(x).as_posix()
                        for x in self.backend.get_custom_target_sources(target)
                        if x.endswith(".rc")
                    ],
                    "resources": [x for x in target.get_outputs()],
                },
            )
        )

    def build_outputs(self, outputs: T.Set[str]):
        cmd = ["ninja", "-C", str(self.resolver.shadow_dir)]
        cmd += [output for output in outputs]
        try:
            subprocess.check_call(cmd)
        except subprocess.SubprocessError as se:
            mlog.warning(f"Failed to generate {outputs} due to {se}")

    def generate(self, target: build.CustomTarget) -> BazelRule:
        if self.library.is_registered(target.name):
            return self.library.get(target.name)

        if self.DEBUG_LOG:
            mlog.debug(f"custom_target_command_as_bazel({target.name}) ")

        # Check to see if this could be a py_binary:
        # -> the command starts with a python interpreter, 2nd is a .py file
        # -> the command starts with a .py file
        cmds = target.command.copy()
        srcs = set()
        tools = []

        if isinstance(cmds[0], build.Executable):
            label = as_bazel_label(cmds[0].name)
            cmds[0] = f"$(location :{label})"
            tools = [f":{label}"]
        elif is_python_exe(cmds[0]):
            rule = self.create_py_binary(target)
            if not rule.is_valid():
                mlog.warning(
                    f"Rule {rule.name} is invalid, not generating {target.name}"
                )
                if self.DEBUG_LOG:
                    mlog.debug(f"Broken rule {rule}")
                return

            cmds.pop(0)
            cmds[0] = f"$(location :{rule.name})"
            tools = [f":{rule.name}"]
            # It looks like we need to propagate the [data] section
            # to the dependencies of the custom command.
            srcs.update(rule.params.get("data", []))
        elif Path(cmds[0]).suffix == ".py":
            rule = self.create_py_binary(target)
            if not rule.is_valid():
                mlog.warning(
                    f"Rule {rule.name} is invalid, not generating {target.name}"
                )
                if self.DEBUG_LOG:
                    mlog.debug(f"Broken rule {rule}")
                return

            cmds[0] = f"$(location :{rule.name})"
            tools = [f":{rule.name}"]
        elif isinstance(cmds[0], str):
            if is_resource_compiler(cmds[0]):
                return self.create_resource_rule(target)
        else:
            # TODO: Add support for shell scripts and executables that are created
            # as part of the build
            mlog.error(
                f"Genrule {cmds} {type(cmds[0])} for {target.name}: not supported, ignoring"
            )
            return

        outdir = Path(self.backend.get_custom_target_output_dir(target))
        outs = set([Path.joinpath(outdir, i).as_posix() for i in target.get_outputs()])
        # Next let's make sure these are generated in the shadow directory, so the build generator
        # can consume the generated sources
        self.build_outputs(outs)

        outputs = [f"$(location {i})" for i in outs]
        inputs = [
            self.resolver.find(x).as_posix()
            for x in self.backend.get_custom_target_sources(target)
        ]
        srcs.update(inputs)
        inputs = [
            f"$(location {s})" if os.path.isfile(s) else f"$(RULEDIR)/{s}"
            for s in inputs
        ]

        if target.capture:
            cmds.append(f"> {outputs[0]}")

        # Evaluate the command list
        if self.DEBUG_LOG:
            mlog.debug(f"     outputs: {outputs}")
        if self.DEBUG_LOG:
            mlog.debug(f"     inputs:  {inputs}")

        cmd: T.List[str] = []
        for i in cmds:
            if isinstance(i, build.BuildTarget):
                tgt = as_bazel_label(i.name)
                if self.DEBUG_LOG:
                    mlog.debug(f"     i-> build.BuildTarget:: {i}")
                cmd += f"$(location {tgt})"
                tools.append(f"{tgt}")
                continue
            elif isinstance(i, build.CustomTarget):
                # GIR scanner will attempt to execute this binary but
                # it assumes that it is in path, so always give it a full path.
                i = i.get_outputs()[0]
                if self.DEBUG_LOG:
                    mlog.debug(f"     i-> build.CustomTarget:: {i}")
            elif isinstance(i, File):
                i = f"$(location {self.resolver.find(i)})"
                if self.DEBUG_LOG:
                    mlog.debug(f"     i-> File: {i}")

            elif isinstance(i, str):
                if any(
                    token in i
                    for token in [
                        "@SOURCE_ROOT@",
                        "@BUILD_ROOT@",
                        "@CURRENT_SOURCE_DIR@",
                        "@DEPFILE@",
                        "@PRIVATE_DIR@",
                    ]
                ):
                    raise MesonBugException(
                        f"Unsupported token ({i}) in custom command."
                    )

                # So everything has to be completely self contained in a bazel build
                # file. So if we are running custom-generator scripts we have basically
                # 2 variants:
                # - It is the invocation of find_program, with a set of parameters.
                # .  - For this case we would have a bazel target we shimmed with a binary
                # - It is something that was build during meson itself (python/binary etc)
                # .  - We do not really know what this is..
                if os.path.exists(i):
                    if os.path.isfile(i):
                        bazel_target = get_bazel_target_from_script(i)
                        if bazel_target:
                            i = f"$(location {bazel_target})"
                            tools.append(bazel_target)
                        else:
                            if self.DEBUG_LOG:
                                mlog.debug(f"     i-> str resolving: {i}")
                            f = self.resolver.find(i)
                            i = f"$(location {f.as_posix()})"
                            srcs.add(f.as_posix())
                    else:
                        i = "$(RULEDIR)"

                if self.DEBUG_LOG:
                    mlog.debug(f"     i-> str: {i}")

            else:
                raise RuntimeError(f"Argument {i} is of unknown type {type(i)}")
            if i == "":
                i = '""'
            cmd.append(i)

        # Substitute the rest of the template strings
        values = get_filenames_templates_dict(inputs, outputs)
        if self.DEBUG_LOG:
            mlog.debug(f"     values: {values}")
        if self.DEBUG_LOG:
            mlog.debug(f"     subst: {cmd}")
        cmd = substitute_values(cmd, values)
        cmd = [i.replace("\\", "/") for i in cmd]
        if self.DEBUG_LOG:
            mlog.debug(f"     subst: {cmd}")

        return self.library.register(
            BazelRule(
                "genrule",
                {
                    "name": f"generate_{target.name}",
                    "srcs": OrderedSet(sorted(srcs)),
                    "tools": tools,
                    "outs": OrderedSet(sorted(outs)),
                    "cmd": " ".join(cmd),
                    "cmd_bat": " ".join(cmd),
                },
            )
        )


class GeneratedListGenerator(CustomTargetGenerator):
    def build_outputs(self, outputs: T.Set[str]):
        cmd = ["ninja", "-C", str(self.resolver.shadow_dir)]
        cmd += [output for output in outputs]
        try:
            subprocess.check_call(
                cmd,
            )
        except subprocess.SubprocessError as se:
            mlog.warning(f"Failed to generate {outputs} due to {se}")

    def create_py_binary(self, target: programs.ExternalProgram):
        """Creates a py_binary rule if needed for this target."""

        # TODO(jansene): This only handles py_binaries right now
        prog = Path(target.get_path()).with_suffix("").name

        # Binary has been created already.
        if self.library.is_registered(prog):
            return self.library.get(prog)

        return self.library.register(
            BazelRule(
                "py_binary",
                {
                    "name": prog,
                    "srcs": OrderedSet(
                        [self.resolver.find(target.get_path()).as_posix()]
                    ),
                },
            )
        )

    def generate(self, genlist: build.GeneratedList, target: build.BuildTarget) -> None:
        for x in genlist.depends:
            if isinstance(x, build.GeneratedList):
                self.generate_genlist_for_target(x, target)

        generator = genlist.get_generator()

        # TODO(jansene): This should be generalized to handle all "exe(s)"
        # but for qemu this is sufficient.
        tools = [self.create_py_binary(generator.get_exe()).name]

        infilelist = genlist.get_inputs()
        outfilelist = genlist.get_outputs()
        # extra_dependencies = self.get_target_depend_files(genlist)
        for i, curfile in enumerate(infilelist):
            if len(generator.outputs) == 1:
                rule_name = f"generate_{target.name}_{outfilelist[i]}"
                target_dir = Path(self.backend.get_target_private_dir(target))
                sole_output = Path.joinpath(target_dir, outfilelist[i]).as_posix()
            else:
                rule_name = f"generate_{target.name}_{curfile}"
                sole_output = Path(curfile).as_posix()

            infilename = self.resolver.find(curfile).as_posix()
            args = generator.get_arglist(infilename)
            self.build_outputs([sole_output])
            args = [
                x.replace("@INPUT@", f"$(location {infilename})").replace(
                    "@OUTPUT@", f"$(location {sole_output})"
                )
                for x in args
            ]
            if len(generator.outputs) > 1:
                outfilelist = outfilelist[len(generator.outputs) :]
            cmdlist = [f"$(location {tools[0]})"] + self.backend.replace_extra_args(
                args, genlist
            )

            self.library.register(
                BazelRule(
                    "genrule",
                    {
                        "name": rule_name,
                        "srcs": OrderedSet([infilename]),
                        "tools": tools,
                        "outs": OrderedSet([sole_output]),
                        "cmd": " ".join(cmdlist),
                        "cmd_bat": " ".join(cmdlist),
                    },
                )
            )

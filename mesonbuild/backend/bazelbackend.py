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

import datetime
import json
import shutil
import platform
import time
import typing as T
from functools import lru_cache
from pathlib import Path

from .. import build, mlog, dependencies
from ..dependencies.pkgconfig import PkgConfigDependency
from ..mesonlib import File, OptionKey, ProgressBar
from .backends import Backend
from .bazel.bazel_rules import BazelRuleLibrary
from .bazel.custom_target_generator import CustomTargetGenerator, GeneratedListGenerator
from .bazel.header_extractor import HeaderExtractor
from .bazel.build_target_generator import BuildTargetGenerator
from .bazel.path_resolver import PathResolver

if T.TYPE_CHECKING:

    from ..interpreter import Interpreter


class BazelBackend(Backend):

    name = "bazel"
    bazel_filename = "BUILD.bazel"

    def __init__(
        self, build: T.Optional[build.Build], interpreter: T.Optional[Interpreter]
    ):
        super().__init__(build, interpreter)
        self.build_dir = Path(self.environment.get_build_dir())
        self.source_dir = Path(self.environment.get_source_dir())
        self.processed_targets = set()
        if self.build_dir.is_relative_to(self.source_dir):
            raise NotImplementedError("Please keep your build directory outside of source dir")

    def load_shims(self):
        shim_f = Path(
            self.environment.coredata.options[OptionKey("backend_shim")].value
        )
        if shim_f.exists() and shim_f.is_file():
            with open(shim_f, "r") as shim_file:
                json_str = "".join(
                    [x for x in shim_file.readlines() if not x.strip().startswith("//")]
                )
                mlog.debug(f"Parsing config: {json_str}")
                return json.loads(json_str)
        return {}

    def closure_rec(self, target, deps):
        if target in deps:
            return deps

        if isinstance(target, build.GeneratedList):
            return

        deps.add(target)

        if hasattr(target, "get_dependencies"):
            tgt_deps = target.get_dependencies()
            for dep in tgt_deps:
                self.closure_rec(dep, deps)

        if hasattr(target, "get_generated_sources"):
            gen = target.get_generated_sources()
            for dep in gen:
                self.closure_rec(dep, deps)

        if hasattr(target, "get_all_link_deps"):
            for dep in target.get_all_link_deps():
                self.closure_rec(dep, deps)

        if hasattr(target, "objects"):
            for obj in target.objects:
                if isinstance(obj, build.ExtractedObjects):
                    self.closure_rec(obj.target, deps)

        return deps

    @lru_cache(maxsize=None)
    def closure(self, target):
        deps = set()
        return self.closure_rec(target, deps)

    def get_target_generated_sources(self, target: build.BuildTarget) -> T.List[File]:
        """
        Returns a dictionary with the keys being the path to the file
        (relative to the build directory) and the value being the File object
        representing the same path.
        """
        srcs: T.List[File] = []
        for gensrc in target.get_generated_sources():
            for s in gensrc.get_outputs():
                rel_src = self.get_target_generated_dir(target, gensrc, s)
                srcs.append(File.from_built_relative(rel_src))
        return srcs

    def generate_generator_list_rules(self, target):
        # CustomTargets have already written their rules and
        # CustomTargetIndexes don't actually get generated, so write rules for
        # GeneratedLists here
        if hasattr(target, "get_generated_sources"):
            for genlist in target.get_generated_sources():
                if isinstance(genlist, (build.CustomTarget, build.CustomTargetIndex)):
                    continue
                self.gen_list_generator.generate(genlist, target)

    @lru_cache(maxsize=None)
    def generate_target(self, target):
        if target.get_id() in self.processed_targets:
            return

        self.processed_targets.add(target.get_id())
        self.generate_generator_list_rules(target)

        if isinstance(target, build.CustomTarget):
            self.custom_target_generator.generate(target)
        elif isinstance(target, build.CustomTargetIndex):
            self.custom_target_generator.generate(target.target)
        elif isinstance(target, build.StaticLibrary):
            self.build_target_generator.generate(target)
        elif isinstance(target, build.Executable):
            self.build_target_generator.generate(target)

    def initialize(self):
        shadow_dir = self.environment.coredata.options[
            OptionKey("backend_shadow_build")
        ].value
        self.shims = self.load_shims()
        self.build_prefix = (
            Path("platform")
            / f"{platform.system().lower()}-{platform.machine().lower()}"
        )
        self.resolver = PathResolver(
            self.source_dir, Path(shadow_dir).absolute(), self.build_dir, self.build_prefix
        )
        mlog.log(f"Using shadow: {shadow_dir}, build_prefix: {self.build_prefix} and build_dir: {self.build_dir}")
        self.library = BazelRuleLibrary(self.shims)
        self.header_extractor = HeaderExtractor(shadow_dir, self.resolver)
        self.custom_target_generator = CustomTargetGenerator(
            self.library, self, self.resolver
        )
        self.gen_list_generator = GeneratedListGenerator(
            self.library, self, self.resolver
        )
        self.build_target_generator = BuildTargetGenerator(
            self.library, self, self.resolver, self.header_extractor
        )

    def verify_external_dependencies(
        self, deps: T.List[dependencies.ExternalDependency]
    ):
        shim_deps = self.shims.get("external_deps", {})
        required: T.Set[PkgConfigDependency] = set()

        for dep in deps:
            if isinstance(dep, PkgConfigDependency):
                required.add(dep)

        missing = []
        for needed in required:
            if needed.name not in shim_deps:
                missing.append(needed.name)

        if missing:
            raise ValueError(
                f"Missing shims for {missing}, please add it to your shim config"
            )

    def write_results(self):
        out_dir = Path(self.build_dir) / self.shims.get("output_dir", "bazel")
        out_dir.mkdir(parents=True, exist_ok=True)

        # Copy all the generated config files
        platform_dir = out_dir / self.build_prefix
        platform_dir.mkdir(parents=True, exist_ok=True)
        for config_file in self.interpreter.configure_file_outputs:
            cfg = Path(config_file)
            dest_dir = Path.joinpath(platform_dir, cfg.parent)
            dest_dir.mkdir(parents=True, exist_ok=True)

            src = self.build_dir / cfg
            dest = dest_dir / cfg.name
            if src.exists():
                shutil.copyfile(src, dest)

        # And write out the bazel build file.
        outfilename = out_dir / self.bazel_filename
        with open(outfilename, "w", encoding="utf-8") as outfile:
            outfile.write(
                f'# This is the build file for project "{self.build.get_project()}"\n'
            )
            outfile.write("# It was autogenerated by the Meson build system.\n")
            outfile.write("# Using the experimental bazel build plugin.\n")
            if platform.system() == "Windows":
                outfile.write("# WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING!\n")
                outfile.write("# This was generated under windows, running buildifier on this file could break this build\n")
                outfile.write("# As windows is not using a sandbox, we rely on the declared includej order.\n")
                outfile.write("# WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING! WARNING!\n")
            outfile.write(self.shims.get("bazel_prefix", ""))
            outfile.write("\n")

            self.library.serialize(outfile)

            outfile.write(self.shims.get("bazel_postfix", ""))
            outfile.write("\n")


    def generate(
        self, capture: bool = False, vslite_ctx: dict = None
    ) -> T.Optional[dict]:
        self.initialize()
        start = time.time()
        target_map = self.build.get_targets()
        targets = target_map.values()
        name_to_target = {}

        for target in targets:
            name_to_target[target.name] = target
            if hasattr(target, "link_whole_targets") and target.link_whole_targets:
                for t in target.link_whole_targets:
                    t.alwayslink = True

            if hasattr(target, "external_deps"):
                self.verify_external_dependencies(target.external_deps)

        self.header_extractor.build_external_dependency_map(targets)

        exports = self.shims.get("export", name_to_target.keys())
        export_targets = set()
        for export in exports:
            export_targets.update(self.closure(name_to_target[export]))

        # Let's order the targets, so runs progress in the same fashion.
        export_targets = sorted(
            [t for t in export_targets], key=lambda target: str(target.name)
        )

        for target in export_targets:
            if target.name not in self.shims.get("exclude", []):
                self.generate_target(target)

        self.library.post_process_rules()
        self.library.apply_shims()
        self.write_results()

        elapsed_timedelta = datetime.timedelta(seconds=time.time() - start)
        mlog.log(f"Completed in {elapsed_timedelta}")

        # self.serialize_tests()
        # self.create_install_data_files()

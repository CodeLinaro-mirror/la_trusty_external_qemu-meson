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

import re
import threading
import typing as T

from ... import mlog
from ...mesonlib import OrderedSet
from ... import build


def as_bazel_label(target: str) -> str:
    # Target names must be composed entirely of characters drawn from the set
    # a–z, A–Z, 0–9, and the punctuation symbols !%-@^_"#$&'()*-+,;<=>?[]{|}~/.
    # We restrict it a bit more for readability
    allowed_chars = set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!%-@^_"
    )

    target_cleaned = "".join(char if char in allowed_chars else "_" for char in target)
    target_cleaned = re.sub("^_+|_+$", "", target_cleaned)

    return target_cleaned


def meson_target_as_bazel_label(target) -> str:
    if isinstance(target, build.StaticLibrary):
        return as_bazel_label(f"lib{target.name}")

    return as_bazel_label(target.name)


class BazelRule:

    DEBUG_LOG = False

    def __init__(self, sort: str, params):
        self.sort = sort
        self.params = params
        self.params["name"] = as_bazel_label(params["name"])

    @property
    def name(self):
        return self.params["name"]

    @name.setter
    def name(self, value):
        self.params["name"] = value

    def is_valid(self):
        if self.DEBUG_LOG:
            mlog.debug(f"Checking {self}")
        srcs = self.params.get("srcs", [])
        data = self.params.get("data", [])
        return not (
            any([x.startswith("/") for x in srcs])
            or any([x.startswith("/") for x in data])
        )

    def __lt__(self, other):
        if self.sort == other.sort:
            return self.name < other.name
        return self.sort < other.sort

    def copy(self):
        return BazelRule(self.sort, self.params.copy())

    def __eq__(self, other):
        return self.name == other.name

    def __str__(self):
        bazel_cmd = "\n"
        for k, v in sorted(self.params.items()):
            if isinstance(v, str):
                v = f"'{v}'"
            if isinstance(v, set) or isinstance(v, OrderedSet):
                v = [x for x in v]
            if v:
                if k == "includes":
                    # Make sure we don't re-order the includes
                    # Windows relies on this.
                    bazel_cmd += "   # buildifier: leave-alone\n"
                bazel_cmd += f"   {k} = {v},\n"
        return f"{self.sort}({bazel_cmd})"


class BazelRuleLibrary:

    DEBUG_LOG = True

    def __init__(self, shims: {}):
        self.library = {}
        self.shims = shims
        self.lock = threading.Lock()

    def _apply_dep_shims(self, rule: BazelRule):
        ext_shims = self.shims.get("external_deps", {})
        updated_deps = OrderedSet()
        for d in rule.params["deps"]:
            updated_deps.update(ext_shims.get(d, [d]))
        rule.params["deps"] = updated_deps

    def _fix_dep_prefix(self, rule: BazelRule):
        updated_deps = OrderedSet()
        for d in sorted(rule.params["deps"]):
            if d[0] in ("@", "/", ":"):
                updated_deps.add(d)
            else:
                # If the target is relative then lets be clearer about that.
                updated_deps.add(":{}".format(d))
        rule.params["deps"] = updated_deps

    def register(self, rule: BazelRule) -> BazelRule:
        with self.lock:
            if rule.name in self.library:
                return self.library[rule.name]

            if self.DEBUG_LOG:
                mlog.debug(f"Registering {rule.name} -> {rule}")

            self.library[rule.name] = rule
            if "deps" in rule.params:
                self._apply_dep_shims(rule)
                self._fix_dep_prefix(rule)

            return rule

    def get(self, name: str) -> BazelRule:
        with self.lock:
            return self.library[as_bazel_label(name)]

    def is_registered(self, name: str) -> bool:
        with self.lock:
            return as_bazel_label(name) in self.library

    def serialize(self, stream):
        for target in sorted(self.library.values()):
            if not target.is_valid():
                mlog.warning(f"Target {target.name} is invalid {target}, ignoring.")
                if self.DEBUG_LOG:
                    mlog.debug(f"Broken target: {target}")
                continue

            stream.write(str(target))
            stream.write("\n")

    def _compile_shim_targets(
        self, shims: T.List[T.Dict[str, str]]
    ) -> T.Dict[re.Pattern, T.List[T.Dict[str, str]]]:
        rule_regex_to_shim = {}
        for shim in shims:
            if "target" in shim:
                regex = re.compile(shim["target"])
                rule_regex_to_shim.setdefault(regex, []).append(shim.get("shims", {}))
        return rule_regex_to_shim

    def _apply_removal_shims(self, rule, param: str, shim_entries: T.Set[str]):
        if param in rule.params:
            to_remove = {
                entry
                for entry in rule.params[param]
                for shim_entry in shim_entries
                if re.match(shim_entry, entry)
            }
            rule.params[param] -= to_remove

    def _apply_addition_shims(self, rule, param: str, shim_entries: T.Set[str]):
        rule.params.setdefault(param, OrderedSet()).update(shim_entries)

    def _apply_replacement_shims(
        self, rule, param: str, shim_entries: T.Union[T.Set[str], str]
    ):
        rule.params[param] = (
            OrderedSet(shim_entries) if isinstance(shim_entries, list) else shim_entries
        )

    def apply_shims(self):
        rule_regex_to_shim = self._compile_shim_targets(self.shims.get("shims", []))

        renames = {}

        for regex, shims in rule_regex_to_shim.items():
            for name, rule in self.library.items():
                if regex.match(name):
                    for shim_dict in shims:
                        restrict_to = shim_dict.get("restrict_to", r".*")
                        if not re.match(restrict_to, rule.sort):
                            if self.DEBUG_LOG:
                                mlog.debug(
                                    f"Shim is restricted_to: {restrict_to}, ignoring {rule.sort}"
                                )
                            continue

                        if self.DEBUG_LOG:
                            mlog.debug(f"Shimming {rule.sort}(name = '{name}')")

                        # For genrules we will try to update cmd if input labels are shimmed.
                        input_replacements = {}

                        for shim_key, shim_value in shim_dict.items():
                            if shim_key == "restrict_to":
                                continue

                            # Change the type of this rule, from cc_binary -> cc_interface_binary etc..
                            if shim_key == "_bzl_type":
                                rule.sort = shim_value
                                continue

                            if shim_key.startswith("-"):
                                param = shim_key[1:]
                                if self.DEBUG_LOG:
                                    mlog.debug(f"Removal shim for: {param}")
                                self._apply_removal_shims(rule, param, shim_value)

                            elif shim_key.startswith("+"):
                                param = shim_key[1:]
                                if self.DEBUG_LOG:
                                    mlog.debug(f"Adding shim for: {param}")
                                self._apply_addition_shims(rule, param, shim_value)

                            else:
                                if self.DEBUG_LOG:
                                    mlog.debug(f"Replacement shim for: {shim_key}")
                                if shim_key == "name":
                                    renames[name] = f":{shim_value}"
                                    renames[f":{name}"] = f":{shim_value}"
                                if shim_key == "srcs" and len(shim_value) == 1:
                                    [new] = shim_value
                                    for old in rule.params["srcs"]:
                                        input_replacements[old] = new
                                if shim_key == "tools" and len(shim_value) == 1:
                                    [new] = shim_value
                                    for old in rule.params["tools"]:
                                        input_replacements[old] = new

                                self._apply_replacement_shims(
                                    rule, shim_key, shim_value
                                )

                        # If srcs or tools were changed on a genrule then patch up the cmd if it wasn't already shimmed.
                        if rule.sort == "genrule" and input_replacements and "cmd" not in shim_dict and "cmd_bat" not in shim_dict:
                            if "cmd" in rule.params:
                                cmd = rule.params["cmd"]
                                for old, new in input_replacements.items():
                                    cmd = cmd.replace(old, new)
                                rule.params["cmd"] = cmd
                            if "cmd_bat" in rule.params:
                                cmd = rule.params["cmd_bat"]
                                for old, new in input_replacements.items():
                                    cmd = cmd.replace(old, new)
                                rule.params["cmd_bat"] = cmd

        # If we've renamed a rule then we should rename the dependency pointing
        # to it from other rules.
        for name, rule in self.library.items():
            if "deps" in rule.params:
                rule.params["deps"] = OrderedSet(
                    d if d not in renames else renames[d] for d in rule.params["deps"]
                )

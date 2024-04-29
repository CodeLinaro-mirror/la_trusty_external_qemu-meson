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


class ExtractionRule(BazelRule):
    def __init__(self, params, deps):
        super().__init__("cc_library", params)
        self.deps = deps

    def copy(self):
        return ExtractionRule(self.params.copy(), self.deps.copy())


class BazelRuleLibrary:

    DEBUG_LOG = True

    def __init__(self, shims: {}):
        self.library = {}
        self.shims = shims

    def _apply_dep_shims(self, rule: BazelRule):
        ext_shims = self.shims.get("external_deps", {})
        updated_deps = OrderedSet()
        for d in rule.params["deps"]:
            updated_deps.update(ext_shims.get(d, [d]))
        rule.params["deps"] = updated_deps

    def register(self, rule: BazelRule) -> BazelRule:
        if self.DEBUG_LOG:
            mlog.debug(f"Registering {rule.name} -> {rule}")
        if rule.name in self.library:
            raise ValueError(f"Rule {rule.name} has been registered already")

        self.library[rule.name] = rule
        if "deps" in rule.params:
            self._apply_dep_shims(rule)

        return rule

    def get(self, name: str) -> BazelRule:
        return self.library[as_bazel_label(name)]

    def is_registered(self, name: str) -> bool:
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

    def _extraction_rules(self) -> T.List[ExtractionRule]:
        return [x for x in self.library.values() if isinstance(x, ExtractionRule)]

    def _rebalance(self, parent, children):
        parent_rule = self.library[parent]
        sources = parent_rule.params["srcs"]
        if self.DEBUG_LOG:
            mlog.debug(f"REBalancing: {parent} - {len(sources)}:  {sources}")

        for child in children:
            child_rule = self.library[child]
            child_sources = child_rule.deps[parent]
            if child_sources == sources:
                # No need to create a target, just take a dependency
                child_rule.params["deps"].add(parent_rule.name)
                continue

            new_intermediate = parent_rule.copy()
            new_intermediate.params["name"] = f"{child_rule.name}_{parent_rule.name}"
            new_intermediate.params["srcs"] = OrderedSet(child_sources)
            child_rule.params["deps"].add(new_intermediate.params["name"])
            self.register(new_intermediate)

    def post_process_rules(self):
        # First we find all the rules from which dependencies select
        # objects.
        extraction_rules = self._extraction_rules()
        rules = {}
        for ex_rule in extraction_rules:
            for target in ex_rule.deps.keys():
                if target not in rules:
                    rules[target] = OrderedSet()
                rules[target].add(ex_rule.name)

        for parent, children in rules.items():
            self._rebalance(parent, children)

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

                        for shim_key, shim_value in shim_dict.items():
                            if shim_key == "restrict_to":
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
                                self._apply_replacement_shims(
                                    rule, shim_key, shim_value
                                )

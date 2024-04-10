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
        if self.DEBUG_LOG: mlog.debug(f"Checking {self}")
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
                bazel_cmd += f"   {k} = {v},\n"
        return f"{self.sort}({bazel_cmd})"


class ExtractionRule(BazelRule):
    def __init__(self, params, deps):
        super().__init__("cc_library", params)
        self.deps = deps

    def copy(self):
        return ExtractionRule(self.params.copy(), self.deps.copy())


class BazelRuleLibrary:

    DEBUG_LOG = False

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
                if self.DEBUG_LOG: mlog.debug(f"Broken target: {target}")
                continue

            stream.write(str(target))
            stream.write("\n")

    def _extraction_rules(self) -> T.List[ExtractionRule]:
        return [x for x in self.library.values() if isinstance(x, ExtractionRule)]

    def _rebalance(self, parent, children):
        parent_rule = self.library[parent]
        sources = parent_rule.params["srcs"]
        if self.DEBUG_LOG: mlog.debug(f"REBalancing: {parent} - {len(sources)}:  {sources}")

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

    def apply_shims(self):
        rule_regex_to_shim: T.Dict[re.Pattern, T.Dict[str, str]] = {}
        for shim in self.shims.get("shims", []):
            if "target" in shim:
                rule_regex_to_shim[re.compile(shim["target"])] = shim.get("shims", {})

        for regex, shims in rule_regex_to_shim.items():
            for name, rule in self.library.items():
                if regex.match(name):
                    # Now let's see if we have the right type
                    restrict = shims.get("restrict_to", ".*")
                    if not re.match(restrict, rule.sort):
                        if self.DEBUG_LOG: mlog.debug(
                            f"Shim is restricted_to: {restrict}, ignoring {rule.sort}"
                        )
                        continue

                    if self.DEBUG_LOG: mlog.debug(f"Shimming {rule.sort}(name = '{name}')")
                    # Okay, let's shim this rule:
                    for shim in shims.keys():
                        if shim == "restrict_to":
                            continue

                        to_shim = shims[shim]
                        if shim.startswith("-"):
                            # remove an entry matching the regex
                            param = shim[1:]
                            if self.DEBUG_LOG: mlog.debug(f"Removal shim for: {param}")
                            if param in rule.params:
                                to_clean = OrderedSet()
                                for entry in rule.params[param]:
                                    for shim_entry in to_shim:
                                        if re.match(shim_entry, entry):
                                            if self.DEBUG_LOG: mlog.debug(f"Removing {entry} from {param}")
                                            to_clean.add(entry)

                                rule.params[param] = rule.params[param].difference(
                                    to_clean
                                )

                        elif shim.startswith("+"):
                            # add an entry matching
                            param = shim[1:]
                            if self.DEBUG_LOG: mlog.debug(f"Adding shim for: {param}")
                            if param not in rule.params:
                                rule.params[param] = OrderedSet()

                            for entry in to_shim:
                                rule.params[param].add(entry)

                        else:
                            if self.DEBUG_LOG: mlog.debug(f"Replacement shim for: {shim}")
                            if isinstance(shim, str):
                                rule.params[shim] = to_shim
                            else:
                                rule.params[shim] = OrderedSet(to_shim)

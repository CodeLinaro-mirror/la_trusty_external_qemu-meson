# Bazel Meson Backend

## Overview

This backend enables the conversion of a Meson project into a Bazel project. Due to inherent differences in philosophies between Meson and Bazel, a direct translation isn't always straightforward. This generator strives to create a functional Bazel project from your Meson build, while acknowledging potential limitations.

## Key Differences

- Meson: Introspects the host, toolchains, and source packages, dynamically configuring the build. This can break hermeticity (reproducibility).
- Bazel: Requires explicit declaration of all input and output files. Meson allows declaring libraries with file sets and include directories, without specifying individual header dependencies. The backend focuses on extracting these dependencies for Bazel compatibility.
- External Dependencies: Meson's automatic dependency resolution needs explicit translation in Bazel. You must provide mappings for each external dependency (e.g., glib-2.0).

### Usage

Invoke with Parameters:

```Bash
meson build --backend bazel \
           -Dbackend_shadow_build=/tmp/build \
           -Dbackend_shim=.../shim.jsonc
```

- `--backend bazel`: Activates the Bazel backend.
- `-Dbackend_shadow_build`: Path to a pre-configured shadow build for custom targets generating sources.
- `-Dbackend_shim`: Path to a JSONC shim file for additional configuration (explained below).

### Shim File Structure

```jsonc
{
   // Note, comments start with "//" and should be the first characters.
   // These are shims we should apply to the various targets
    "shims": [
        {
            // Fot the following bazel target:
            "target": "shared_QAPI_source_files",
            "shims": {
                // We replace "cmd" with the following:
                "cmd": "$(location :qapi-gen) -o $(RULEDIR)/qapi -b $(location qapi/qapi-schema.json)"
            }
        },
        {
            "target": "keymap-gen",
            "shims": {
                "srcs": [
                    "subprojects/keycodemapdb/tools/keymap-gen.py"
                ]
            }
        },
        {
            "target": "qemu-system-riscv64",
            "shims": {
                // add the following set of dependencies to
                // the qemu-system-riscv64 target
                "+deps" : ["qemu-riscv64-softmmu"]
            }
        },
        {
            "target": "qemu-system-aarch64",
            "shims": {
                "+deps" : ["qemu-aarch64-softmmu"]
            }
        },
        {
            "target": "qemu-system-x86_64",
            "shims": {
                "+deps" : ["qemu-x86_64-softmmu"]
            }
        },
        {
            "target": ".*",
            "shims": {
                // We restrict this shim to cc_library, cc_executable, etc..
                "restrict_to": "cc_.*",
                // Remove the following headers from every "cc_.*" like rule:
                "-hdrs": [
                    "google.*",
                    "subprojects.*"
                ],
                // And make every rule visbile to all
                "visibility": [
                    "//visibility:public"
                ],
                // Remove the following sources from the "srcs"
                "-srcs": [
                    "google.*"
                ],
                // Our sysroot is too old so we need to introduce these options
                "+copts": [
                    "-DF_ADD_SEALS=1033",
                    "-DF_GET_SEALS=1034",
                    "-DF_SEAL_SEAL=0x0001",
                    "-DF_SEAL_SHRINK=0x0002",
                    "-DF_SEAL_GROW=0x0004",
                    "-DF_SEAL_WRITE=0x0008",
                    "-DF_SEAL_EXEC=0x0020",
                    // We do not have memfd_create
                    "-Wno-implicit-function-declaration"
                ],
                // Not yet clear how to handle object includes,
                // so let's strip them.
                "-deps": [
                    "common_.*"
                ],
                // We need a compat layer for our ye olde sysroot.
                "+deps": [
                    "//external/qemu/google/compat/linux:compat"
                ]
            }
        }
        // Sample shim that show cases how you can
        // add and remove dependencies to all cc_ like
        // rules.
        // {
        //     "target" : ".*",
        //     "shims" :{
        //    Use restrict_to to limit this shim to rules
        //    of the given type. For example cc_library, cc_binary
        //         "restrict_to" : "cc_.*",
        //    Add the following items to the "deps" set
        //         "+deps": ["to_be_added"],
        //    remove these items from the "deps" set
        //         "-deps": ["block"]
        //    replace the "deps" completely:
        //        "deps": ["a", "b"]
        //     }
        // }
    ],
    // These are the bazel mappings we are going to use for
    // external dependencies. For every pkg config dependency
    // you will need to provide a bazel target
    "external_deps": {
        "glib-2.0": [
            "@glib//:glib-static",
            "@glib//:gmodule-static"
        ],
        "zlib": [
            "@zlib//:zlib"
        ],
        "pixman-1": [
            "@pixman//:pixman-1",
            "@pixman//:pixman_simd"
        ],
        "libfdt": [
            "//external/dtc:libfdt"
        ],
        "libpulse": [
            "@pulse//:libpulse"
        ],
        "vhost-user": [
            "//external/qemu/subprojects/libvhost-user:vhost-user"
        ],
        "vhost-user-glib": [
            "//external/qemu/subprojects/libvhost-user:vhost-user-glib"
        ],
        // We do not yet have bazel build for rutabaga..
        "rutabaga_gfx_ffi": []
    },
    // The actual set of targets you wish to generate bazel targets for.
    // These are the targets (and dependencies) that will be exported in generated BUILD.bazel file
    "export": [
        "qemu-system-x86_64",
        "qemu-system-aarch64",
        "qemu-system-riscv64"
    ],
    "exclude": [
        "vhost-user",
        "vhost-user-glib"
    ],
    // Directory where the conversion will be written to.
    "output_dir" : "bazel"
}
```

### Output

After successful execution, you'll find a `output_dir`  directory with the following:

- `BUILD.bazel`: The generated Bazel build file.
- Configuration files (specific to your platform) in the `platform/{platform}-{aarch}` dirctory.

## Known Issues and Limitations

- The backend is actively under development, so expect changes and potential issues:
  - We cannot handle all sort of custom targets.
  - We currently need a shadow build directory
  - Do not use a build/shadow build directory that is a symlink (for ex. /tmp -> /private/tmp)
  - We can only translate to a single architecture. If you wish to use multiple platforms you will need a merge tool to merge various bazel files for different targets.
  - Sandboxing on windows is **not available**. This means that the
    include order generated by the meson translator is important.
    Be aware that running buildifier can result in include re-ordering, which in turn could break your build.
  - ...

By following these guidelines and understanding the core concepts, you can leverage this backend to bridge the gap between Meson and Bazel in your projects.

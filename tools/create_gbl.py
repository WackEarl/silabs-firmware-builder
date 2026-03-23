#!/usr/bin/env python3
"""Tool to create a GBL image in a Simplicity Studio build directory."""

from __future__ import annotations

import os
import ast
import sys
import json
import pathlib
import argparse
import subprocess

from ruamel.yaml import YAML


yaml = YAML(typ="safe")


def parse_c_header_defines(file_content: str) -> dict[str, str]:
    """
    Parses a C header file's `#define`s.
    """
    config = {}

    for line in file_content.split("\n"):
        if not line.startswith("#define"):
            continue

        _, *key_value = line.split(None, 2)

        if len(key_value) == 2:
            key, value = key_value
        else:
            key, value = key_value + [None]

        try:
            config[key] = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass

    return config


def parse_properties_file(file_content: str) -> dict[str, str | list[str]]:
    """
    Parses custom .properties file format into a dictionary.
    Handles double backslashes as escape characters for spaces.
    """
    properties = {}

    for line in file_content.split("\n"):
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        key, value = line.split("=", 1)
        key = key.strip()

        properties[key] = []
        current_value = ""
        i = 0

        while i < len(value):
            if value[i : i + 2] == "\\\\":
                current_value += " "
                i += 2
            elif value[i] == " ":
                properties[key].append(current_value)
                current_value = ""
                i += 1
            else:
                current_value += value[i]
                i += 1

        if current_value:
            properties[key].append(current_value)

    return properties


def find_file_in_parent_dirs(root: pathlib.Path, filename: str) -> pathlib.Path:
    """
    Finds a file in the given directory or any of its parents.
    """
    root = root.resolve()

    while True:
        if (root / filename).exists():
            return root / filename

        if root.parent == root:
            raise FileNotFoundError(
                f"Could not find {filename} in any parent directory"
            )

        root = root.parent


def find_sdk_version(sdk_meta: dict, component: str) -> str:
    for x in sdk_meta["documentation"]:
        if x["docset"] == component:
            return x["version"]

    raise RuntimeError(f"Cannot find version for {component}")


def main():
    # Run as a Simplicity Studio post-build step
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("command", type=str, help="Command to execute: postbuild")
    parser.add_argument("slpb_file", type=pathlib.Path, help="Path to the .slpb file")
    parser.add_argument(
        "--parameter",
        action="append",
        type=lambda kv: kv.split(":"),
        dest="parameters",
        help="Parameters in the format key:value",
    )

    args = parser.parse_args()
    args.parameters = dict(args.parameters)

    project_name = args.slpb_file.stem
    build_dir = pathlib.Path(args.parameters["build_dir"])
    out_file = build_dir / f"{project_name}.out"

    artifact_root = out_file.parent
    project_name = out_file.stem
    slcp_path = find_file_in_parent_dirs(
        root=artifact_root,
        filename=project_name + ".slcp",
    )

    project_root = slcp_path.parent

    if "sdk_dir" in args.parameters:
        gsdk_path = pathlib.Path(args.parameters["sdk_dir"])
    elif "cmake" in str(build_dir):
        gsdk_path = pathlib.Path(
            pathlib.Path(build_dir / f"{project_name}.cmake")
            .read_text()
            .split('set(SDK_PATH "', 1)[1]
            .split('"', 1)[0]
        )
    else:
        raise RuntimeError("Cannot determine SDK directory")

    # Parse the main Simplicity Studio SLCS
    sdk_file = next(gsdk_path.glob("*_sdk.slcs"))
    sdk_meta = yaml.load(sdk_file.read_text())
    sdk_version = sdk_meta["sdk_version"]
    gbl_metadata = yaml.load((project_root / "gbl_metadata.yaml").read_text())
    fw_type = gbl_metadata.get("fw_type")

    # Prepare the GBL metadata
    metadata = {
        "metadata_version": 3,
        "sdk_version": sdk_version,
        "fw_type": fw_type,
        "fw_variant": gbl_metadata.get("fw_variant"),
        "baudrate": gbl_metadata.get("baudrate"),
    }

    if fw_type == "zigbee_ncp" or fw_type == "zigbee_router":
        metadata["fw_version"] = find_sdk_version(sdk_meta, "zigbee")
    elif fw_type == "openthread_rcp":
        metadata["fw_version"] = find_sdk_version(sdk_meta, "openthread")
    elif fw_type == "gecko-bootloader":
        # not currently in SLCS
        btl_config_h = parse_c_header_defines(
            (
                gsdk_path / "platform/bootloader/config/btl_config.h"
            ).read_text()
        )

        metadata["fw_version"] = (
            f"{btl_config_h['BOOTLOADER_VERSION_MAIN_MAJOR']}.{btl_config_h['BOOTLOADER_VERSION_MAIN_MINOR']}.{btl_config_h['BOOTLOADER_VERSION_MAIN_CUSTOMER']}"
        )

    print("Generated GBL metadata:", metadata, flush=True)

    # Write it to a file for `commander` to read
    (artifact_root / "gbl_metadata.json").write_text(
        json.dumps(metadata, sort_keys=True)
    )

    # Make sure the Commander binary is included in the PATH on macOS
    if sys.platform == "darwin":
        os.environ["PATH"] += (
            os.pathsep
            + "/Applications/Simplicity Studio.app/Contents/Eclipse/developer/adapter_packs/commander/Commander.app/Contents/MacOS"
        )

    commander_args = [
        "commander",
        "gbl",
        "create",
        out_file.with_suffix(".gbl"),
        (
            "--app"
            if gbl_metadata.get("fw_type", None) != "gecko-bootloader"
            else "--bootloader"
        ),
        out_file,
    ] + (
        [
            "--metadata",
            (artifact_root / "gbl_metadata.json"),
        ]
        if gbl_metadata
        else []
    )

    if gbl_metadata.get("compression", None) is not None:
        commander_args += ["--compress", gbl_metadata["compression"]]

    if gbl_metadata.get("sign_key", None) is not None:
        commander_args += ["--sign", gbl_metadata["sign_key"].format(SDK_DIR=gsdk_path)]

    if gbl_metadata.get("encrypt_key", None) is not None:
        commander_args += [
            "--encrypt",
            gbl_metadata["encrypt_key"].format(SDK_DIR=gsdk_path),
        ]

    # Finally, generate the GBL
    subprocess.run(commander_args, check=True)


if __name__ == "__main__":
    main()

"""Reject failures, xfails and unreviewed Linux skips in a backend JUnit report."""
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

DOCKER_OPT_IN = {
    "test_linux_docker_bind_mount_fd_and_uid",
    "test_linux_docker_named_volume_fd_and_inode",
    "test_linux_docker_container_root_rejected_by_peer_check",
}


def verify(path: Path) -> dict:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    if not cases:
        raise ValueError("Empty backend test report")
    skips = []
    for case in cases:
        name = case.get("name", "")
        if case.find("failure") is not None or case.find("error") is not None:
            raise ValueError(f"Backend failure: {name}")
        skipped = case.find("skipped")
        if skipped is not None:
            # These three tests require an explicitly provisioned isolated Docker
            # host/UID setup. They are collected, reported, and never called PASS.
            if (case.get("classname", "").split(".")[-1] != "test_pra_a1_receipts"
                    or name not in DOCKER_OPT_IN
                    or skipped.get("type") != "pytest.skip"
                    or skipped.get("message") != "explicit isolated Linux Docker runtime gate"):
                raise ValueError(f"Unexpected skip/xfail: {name}: {skipped.attrib}")
            skips.append(name)
    return {"testcases": len(cases), "explicit_docker_opt_in_skips": skips}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    print(verify(parser.parse_args().report))

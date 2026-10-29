"""Operator-export fixtures using the server's published build.json field names."""

import hashlib
import json


def release_record(release="v1.0.0", digest="a"):
    commit = "1" * 40
    build = {
        "build": release,
        "server_runtime": "2" * 64,
        "server_digest": "3" * 64,
        "minecraft": "1.21.1",
        "fabric": "0.16.14",
        "client": "4" * 64,
        "client_version": "v1.0.0",
        "compatibility": "5" * 64,
        "sources": {"server": commit},
    }
    payload = json.dumps(build, sort_keys=True)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    image_config = json.dumps(
        {
            "config": {
                "Labels": {
                    "org.opencontainers.image.source": "https://github.com/Blockfield/blockfield-server",
                    "io.blockfield.server-digest": release,
                }
            }
        }
    )
    image_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {
                "digest": "sha256:" + hashlib.sha256(image_config.encode()).hexdigest(),
            },
            "layers": [{"digest": "sha256:" + digest * 64}],
        }
    )
    image = (
        "ghcr.io/blockfield/blockfield-server@sha256:"
        + hashlib.sha256(image_manifest.encode()).hexdigest()
    )
    return {
        "schema": 1,
        "revision": 1,
        "release": release,
        "imageDigest": image,
        "serverRuntime": {
            "fingerprint": build["server_runtime"],
            "serverDigest": build["server_digest"],
            "minecraft": build["minecraft"],
            "fabric": build["fabric"],
        },
        "clientContract": {
            "digest": build["client"],
            "version": build["client_version"],
            "compatibility": build["compatibility"],
        },
        "source": {
            "repository": "Blockfield/blockfield-server",
            "tag": release,
            "commit": commit,
            "publishedAt": "2026-10-29T12:00:00Z",
            "draft": False,
            "prerelease": False,
            "buildJson": payload,
            "checksums": checksum + "  build.json\n",
            "imageManifest": image_manifest,
            "imageConfig": image_config,
            "provenance": {
                "imageDigest": image,
                "sourceCommit": commit,
                "buildSha256": checksum,
            },
        },
    }


def catalog():
    return {
        f"v{n}.0.0": release_record(f"v{n}.0.0", d)
        for n, d in [(1, "a"), (2, "b"), (3, "c")]
    }

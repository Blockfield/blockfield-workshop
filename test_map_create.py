"""Private zero-map creation, first publication and Workshop-only GUI operations."""

import copy
import io
import json
import os
import stat
import struct
import unittest
import zlib
from unittest import mock
from uuid import uuid4

import candidates
import map_package
from controller import Goal
import test_workshop_sessions as fixtures

ACTOR = {"draslId": "c" * 32, "name": "Creator"}


def playable(map_id="new_map", modes=None):
    config = map_package.seed_config(
        map_id, "New map", list(map_package.SUPPORTED_MODES) if modes is None else modes
    )
    data = config["mode_data"]
    if "campaign" in data:
        data["campaign"] = {
            "bases": {
                "RedTeam": {"x": 4, "y": 65, "z": 4},
                "BlueTeam": {"spawn_points": [{"x": 12, "y": 65, "z": 12}]},
            },
            "points": [{"id": "A", "name": "A", "x": 8, "y": 65, "z": 8}],
        }
    if "deathmatch" in data:
        data["deathmatch"]["spawn_points"] = [
            {"x": 4, "y": 65, "z": 4},
            {"x": 12, "y": 65, "z": 12},
        ]
    if "training" in data:
        data["training"]["targets"] = [
            {"id": "target_1", "x": 10, "y": 65, "z": 10, "armor": "none"}
        ]
    return config


class ZeroMapTests(unittest.TestCase):
    def setUp(self):
        fixtures.WorkshopSessionTests.setUp(self)
        self.controller.set_members([ACTOR | {"role": "admin"}])
        self.backend.events.clear()
        self.game = self.backend.processes[0]

    def create(self, map_id="new_map", modes=None, session_id=None):
        return self.store.create_map(
            map_id,
            "New map",
            ["deathmatch"] if modes is None else modes,
            session_id or str(uuid4()),
            ACTOR,
        )

    def commit(self, created, config):
        session = created["session"]["id"]
        bundle = self.store.versions.working_copy(created["mapId"], session)
        (bundle / "map.json").write_text(json.dumps(config))
        return self.store.commit(
            created["mapId"],
            ACTOR,
            "Complete layout",
            working_copy_id=session,
            branch=created["branch"]["name"],
            expected_head=created["branch"]["versionDigest"],
        )

    def review(self, created, committed):
        return self.store.review(
            created["mapId"],
            ACTOR,
            "Review layout",
            working_copy_id=created["session"]["id"],
            branch=created["branch"]["name"],
            expected_head=committed["versionDigest"],
        )

    def request(self, kind, **fields):
        return self.controller._apply(
            str(uuid4()),
            {
                "kind": kind,
                "actor": ACTOR,
                "expectedRevision": self.controller.status()["revision"],
                **fields,
            },
        )

    def ready(self):
        self.backend.ready(Goal(**self.controller.status()["desired"]))
        self.controller.step()

    def test_all_seven_modes_create_only_private_incomplete_drafts(self):
        for mode in map_package.SUPPORTED_MODES:
            with self.subTest(mode=mode):
                created = self.create("new_" + mode, [mode])
                config = self.store.versions._config(
                    self.store.versions.read(
                        created["mapId"], created["versionDigest"]
                    )["files"]
                )
                self.assertEqual(config["supported_modes"], [mode])
                self.assertEqual(config["kind"], "arena")
                self.assertIsNone(self.store.versions.head(created["mapId"]))
                self.assertFalse((self.store.templates / created["mapId"]).exists())
                self.assertFalse(
                    (self.store.published / (created["mapId"] + ".json")).exists()
                )
                self.assertEqual(
                    self.store.session(created["session"]["id"]), created["session"]
                )
                with self.assertRaises(ValueError):
                    self.store.versions.activate(
                        created["mapId"], created["versionDigest"], ACTOR, None
                    )
                with self.assertRaises(ValueError):
                    self.review(created, created)
                map_package.validate_playable(playable(created["mapId"], [mode]))

    def test_seed_has_real_anvil_palette_platform_and_void_sections(self):
        raw = map_package.seed_region()
        self.assertEqual(len(raw) % 4096, 0)
        self.assertEqual(int.from_bytes(raw[:3], "big"), 2)
        length = int.from_bytes(raw[8192:8196], "big")
        self.assertEqual(raw[8196], 2)
        stream = io.BytesIO(zlib.decompress(raw[8197 : 8196 + length]))

        def read(fmt):
            return struct.unpack(fmt, stream.read(struct.calcsize(fmt)))[0]

        def text():
            return stream.read(read(">H")).decode()

        def payload(tag):
            if tag in {1, 3, 4}:
                return read({1: ">b", 3: ">i", 4: ">q"}[tag])
            if tag == 8:
                return text()
            if tag == 9:
                kind, count = read(">b"), read(">i")
                return [payload(kind) for _ in range(count)]
            if tag == 10:
                result = {}
                while kind := read(">b"):
                    name = text()
                    result[name] = payload(kind)
                return result
            if tag == 12:
                return [read(">q") for _ in range(read(">i"))]
            self.fail("unexpected NBT tag")

        self.assertEqual((read(">b"), text()), (10, ""))
        chunk = payload(10)
        self.assertEqual(chunk["DataVersion"], 3955)
        self.assertEqual(
            (chunk["xPos"], chunk["zPos"], chunk["Status"]), (0, 0, "minecraft:full")
        )
        self.assertEqual([s["Y"] for s in chunk["sections"]], list(range(-4, 20)))
        for section in chunk["sections"]:
            state = section["block_states"]
            if section["Y"] != 4:
                self.assertEqual(state, {"palette": [{"Name": "minecraft:air"}]})
                continue
            self.assertEqual(state["palette"][1]["Name"], "minecraft:stone")
            indices = [
                (word >> offset) & 15
                for word in state["data"]
                for offset in range(0, 64, 4)
            ]
            self.assertEqual(indices, [1] * 256 + [0] * 3840)
            for mode in map_package.SUPPORTED_MODES:
                bounds = map_package.seed_config("new_" + mode, "New map", [mode])[
                    "bounds"
                ]
                x = int((bounds["minX"] + bounds["maxX"]) / 2)
                z = int((bounds["minZ"] + bounds["maxZ"]) / 2)
                with self.subTest(mode=mode):
                    self.assertTrue(0 <= x < 16 and 0 <= z < 16)
                    self.assertEqual(
                        state["palette"][indices[z * 16 + x]]["Name"], "minecraft:stone"
                    )
        self.assertEqual(stream.read(), b"")

    def test_collision_invalid_properties_and_uuid_rebinding_make_no_history(self):
        for map_id in ("arena", "hub", "lobby", "limbo", "../bad", "Bad", "a" * 33):
            with self.subTest(map_id=map_id), self.assertRaises(ValueError):
                self.create(map_id)
        for modes in ([], ["unknown"], ["deathmatch", "deathmatch"], "deathmatch"):
            with self.subTest(modes=modes), self.assertRaises(ValueError):
                self.create(modes=modes)
        for name in ("", " ", "a" * 101, "bad\nname", "§red"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.create_map(
                    "new_map", name, ["deathmatch"], str(uuid4()), ACTOR
                )
        old = self.controller.session()["id"]
        with self.assertRaisesRegex(ValueError, "bound"):
            self.create(session_id=old)
        self.assertNotIn("new_map", self.store.map_ids())
        created = self.create()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.create()
        with self.assertRaisesRegex(ValueError, "different map properties"):
            self.store.create_map(
                "new_map", "Other", ["deathmatch"], created["session"]["id"], ACTOR
            )

    def test_failed_checkout_leaves_catalog_valid_and_same_uuid_retry_preserves_dirty_copy(
        self,
    ):
        session = str(uuid4())
        before = self.store.catalog()
        with mock.patch.object(
            self.store.versions, "checkout", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaisesRegex(OSError, "ENOSPC"):
                self.create(session_id=session)
        self.assertEqual(self.store.catalog(), before)
        self.assertIsNone(self.store.session(session))
        created = self.create(session_id=session)
        copy = self.store.versions.working_copy("new_map", session)
        region = copy / "world/region/r.0.0.mca"
        region.write_bytes(b"unsaved work")
        self.assertEqual(self.create(session_id=session), created)
        self.assertEqual(region.read_bytes(), b"unsaved work")

    def test_low_disk_refuses_creation_before_save_and_missing_archived_ref_is_not_recreated(
        self,
    ):
        before = self.controller.status()
        with mock.patch("candidates.shutil.disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(OSError, "space"):
                self.request(
                    "map-create",
                    mapId="new_map",
                    name="New map",
                    supportedModes=["deathmatch"],
                    sessionId=str(uuid4()),
                )
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.backend.events, [])
        created = self.create()
        ref = self.store.versions._branch_path("new_map", created["branch"]["name"])
        ref.unlink()
        with self.assertRaises((OSError, ValueError)):
            self.create(session_id=created["session"]["id"])
        self.assertFalse(ref.exists())

    def test_private_commit_needs_no_template_and_initial_publish_is_exact_null_cas(
        self,
    ):
        created = self.create(modes=list(map_package.SUPPORTED_MODES))
        incomplete = self.commit(
            created,
            map_package.seed_config(
                "new_map", "New map", list(map_package.SUPPORTED_MODES)
            ),
        )
        self.assertIsNone(self.store.versions.head("new_map"))
        with self.assertRaises(ValueError):
            self.review(created, incomplete)
        created["branch"] = incomplete["branch"]
        committed = self.commit(created, playable())
        review = self.review(created, committed)
        self.assertIsNone(review["targetHead"])
        preview = self.controller._merge_preview(
            "new_map", review["versionDigest"], None
        )
        self.assertIsNone(preview["baseVersion"])
        self.assertIsNone(preview["targetVersion"])
        self.assertEqual(preview["conflicts"], [])
        with self.assertRaisesRegex(ValueError, "explicit expectedHead"):
            self.store.publish(review["id"], ACTOR, force=True)
        with self.assertRaisesRegex(ValueError, "force"):
            self.store.publish(review["id"], ACTOR, expected_head=None, force=True)
        with mock.patch.object(
            self.store, "activate", side_effect=OSError("activation unavailable")
        ):
            with self.assertRaisesRegex(OSError, "activation unavailable"):
                self.store.publish(review["id"], ACTOR, expected_head=None)
        self.assertIsNone(self.store.versions.head("new_map"))
        accepted = self.store.publish(review["id"], ACTOR, expected_head=None)
        self.assertEqual(accepted["mergeVersion"], committed["versionDigest"])
        self.assertEqual(
            self.store.versions.head("new_map")["versionDigest"],
            committed["versionDigest"],
        )
        with self.assertRaisesRegex(ValueError, "stale map head"):
            self.store.publish(review["id"], ACTOR, expected_head=None)

    def test_archive_sync_failure_retains_complete_private_map_for_same_uuid_retry(
        self,
    ):
        session = str(uuid4())
        archive_dir = self.store.versions.archive / "sessions"
        sync = candidates._sync_directory

        def fail_after_binding(path):
            if path == archive_dir and self.store._session_path(session).exists():
                raise OSError("archive fsync unavailable")
            return sync(path)

        with mock.patch.object(
            candidates, "_sync_directory", side_effect=fail_after_binding
        ):
            with self.assertRaisesRegex(OSError, "archive fsync"):
                self.create(session_id=session)
        archived = self.store.session(session)
        self.assertEqual(archived["maps"], ["new_map"])
        self.assertEqual(
            self.store._session_path(session).stat().st_mode & 0o777, 0o444
        )
        self.store.versions.validate_working_copy("new_map", session)
        self.assertIsNone(
            next(item for item in self.store.catalog() if item["mapId"] == "new_map")[
                "head"
            ]
        )
        self.assertEqual(self.create(session_id=session)["session"], archived)

    def test_selected_test_mode_is_frozen_and_unsupported_mode_does_not_stop_editor(
        self,
    ):
        created = self.request(
            "map-create",
            mapId="new_map",
            name="New map",
            supportedModes=["training"],
            sessionId=str(uuid4()),
        )
        self.ready()
        committed = self.commit(created, playable(modes=["training"]))
        review = self.review(created, committed)
        self.backend.events.clear()
        with self.assertRaisesRegex(ValueError, "does not support"):
            self.request(
                "mode", profile="test", candidateId=review["id"], modeId="deathmatch"
            )
        self.assertEqual(self.backend.events, [])
        self.request(
            "mode", profile="test", candidateId=review["id"], modeId="training"
        )
        desired = self.controller.status()["desired"]
        self.assertEqual(desired["modeId"], "training")
        self.controller.step()
        self.assertEqual(self.backend.events, ["save", "stop", "start"])
        self.ready()
        self.assertEqual(self.controller.status()["gate"], "testers")
        self.assertIs(self.backend.processes[0], self.game)

    def test_legacy_state_missing_mode_field_stays_ready_without_restart(self):
        state = self.controller.status()
        state["desired"].pop("modeId")
        self.controller.file.write_text(json.dumps(state))
        self.assertEqual(self.controller.step()["gate"], "builders")
        self.assertEqual(self.backend.events, [])
        for goal in (
            {
                "profile": "workshop",
                "release": "v1",
                "session": str(uuid4()),
                "modeId": "training",
            },
            {
                "profile": "test",
                "release": "v1",
                "session": str(uuid4()),
                "candidate": "a" * 64,
                "modeId": "unknown",
            },
        ):
            with self.assertRaises(ValueError):
                Goal(**goal)

    def test_rename_stale_or_wrong_copy_is_refused_before_save(self):
        session = self.controller.session()["id"]
        for fields in (
            {"workingCopyId": str(uuid4())},
            {"workingCopyId": session, "expectedRevision": -1},
            {"workingCopyId": session, "mapId": "missing"},
        ):
            with self.assertRaises(ValueError):
                self.request(
                    "map-properties", **({"mapId": "arena", "name": "Renamed"} | fields)
                )
        self.assertEqual(self.backend.events, [])

    def test_frozen_candidate_modes_ignore_later_mutable_configuration(self):
        created = self.create()
        committed = self.commit(created, playable(modes=["deathmatch"]))
        review = self.review(created, committed)
        bundle = self.store.versions.working_copy("new_map", created["session"]["id"])
        (bundle / "map.json").write_text(json.dumps(playable(modes=["training"])))
        self.assertEqual(
            self.store.validate_test(review["id"], "deathmatch"), ["deathmatch"]
        )
        self.assertEqual(
            next(
                item
                for item in self.store.export()["candidates"]
                if item["id"] == review["id"]
            )["supportedModes"],
            ["deathmatch"],
        )
        with self.assertRaisesRegex(ValueError, "does not support"):
            self.store.validate_test(review["id"], "training")

    def test_creation_starts_new_editor_preserves_old_draft_and_game(self):
        previous = self.controller.session()
        old_region = (
            self.store.versions.working_copy("arena", previous["id"])
            / "world/region/r.0.0.mca"
        )
        old_region.write_bytes(b"old unsaved work")
        session = str(uuid4())
        created = self.request(
            "map-create",
            mapId="new_map",
            name="New map",
            supportedModes=["training"],
            sessionId=session,
        )
        self.assertEqual(created["session"]["id"], session)
        self.assertEqual(self.controller.session()["id"], session)
        self.assertEqual(self.backend.events, ["save", "stop", "start"])
        self.assertIs(self.backend.processes[0], self.game)
        self.assertEqual(old_region.read_bytes(), b"old unsaved work")
        self.assertEqual(
            json.loads(self.controller.gate_file.read_bytes())["backends"]["game"][
                "gate"
            ],
            "public",
        )
        self.ready()
        self.assertEqual(self.controller.status()["gate"], "builders")

    def test_permissions_scope_and_stale_revision_refuse_before_save(self):
        fields = {
            "mapId": "new_map",
            "name": "New map",
            "supportedModes": ["deathmatch"],
            "sessionId": str(uuid4()),
        }
        self.controller.set_members(
            [
                ACTOR | {"role": "builder", "maps": ["arena"]},
                {"draslId": "a" * 32, "name": "Admin", "role": "admin"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "lead or admin"):
            self.request("map-create", **fields)
        with self.assertRaisesRegex(ValueError, "authenticated"):
            self.request("map-create", actor=ACTOR | {"name": "Spoofed"}, **fields)
        self.controller.set_members(
            [
                ACTOR | {"role": "lead"},
                {"draslId": "a" * 32, "name": "Admin", "role": "admin"},
            ]
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.request("map-create", expectedRevision=-1, **fields)
        self.assertEqual(self.backend.events, [])
        self.request("map-create", **fields)
        self.assertIs(self.backend.processes[0], self.game)

    def test_create_failure_restarts_previous_editor_without_dead_archived_session(
        self,
    ):
        previous = self.controller.session()
        session = str(uuid4())
        with mock.patch.object(
            self.store.versions, "checkout", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaisesRegex(OSError, "ENOSPC"):
                self.request(
                    "map-create",
                    mapId="new_map",
                    name="New map",
                    supportedModes=["deathmatch"],
                    sessionId=session,
                )
        self.assertEqual(self.controller.session(), previous)
        self.assertIsNone(self.store.session(session))
        self.assertEqual(self.backend.events, ["save", "stop", "start"])
        self.assertIs(self.backend.processes[0], self.game)
        self.ready()
        self.request(
            "map-create",
            mapId="new_map",
            name="New map",
            supportedModes=["deathmatch"],
            sessionId=session,
        )
        self.assertEqual(self.controller.session()["id"], session)

    def test_uncertain_save_never_creates_map_or_restarts_backend(self):
        self.backend.fail_save = True
        with self.assertRaisesRegex(OSError, "save failed"):
            self.request(
                "map-create",
                mapId="new_map",
                name="New map",
                supportedModes=["deathmatch"],
                sessionId=str(uuid4()),
            )
        self.assertEqual(self.backend.events, ["save"])
        self.assertNotIn("new_map", self.store.map_ids())
        self.assertEqual(self.controller.status()["phase"], "ERROR")
        self.assertIs(self.backend.processes[0], self.game)

    def test_rename_changes_only_selected_copy_and_exports_draft_name(self):
        session = self.controller.session()["id"]
        config_path = self.store.versions.working_copy("arena", session) / "map.json"
        before = json.loads(config_path.read_bytes())
        head = self.store.versions.head("arena")
        result = self.request(
            "map-properties", mapId="arena", name=" Renamed ", workingCopyId=session
        )
        self.assertEqual(
            result, {"mapId": "arena", "workingCopyId": session, "name": "Renamed"}
        )
        self.assertEqual(
            json.loads(config_path.read_bytes()), before | {"name": "Renamed"}
        )
        if os.name != "nt":
            self.assertTrue(config_path.stat().st_mode & stat.S_IWGRP)
        self.assertEqual(self.store.versions.head("arena"), head)
        catalog = json.loads((self.root / "admin/maps.json").read_bytes())
        entry = next(item for item in catalog["maps"] if item["mapId"] == "arena")
        self.assertEqual(entry["mapJson"]["name"], "arena")
        self.assertEqual(entry["draft"], {"workingCopyId": session, "name": "Renamed"})
        self.assertEqual(self.backend.events, ["save", "stop", "start"])
        self.assertIs(self.backend.processes[0], self.game)

    def test_failed_atomic_rename_restores_old_editor_and_config(self):
        session = self.controller.session()["id"]
        config = self.store.versions.working_copy("arena", session) / "map.json"
        before = config.read_bytes()
        real = candidates._atomic_json

        def fail_config(path, *args, **kwargs):
            if path == config:
                raise OSError("ENOSPC rename")
            return real(path, *args, **kwargs)

        with mock.patch.object(candidates, "_atomic_json", side_effect=fail_config):
            with self.assertRaisesRegex(OSError, "ENOSPC rename"):
                self.request(
                    "map-properties",
                    mapId="arena",
                    name="Renamed",
                    workingCopyId=session,
                )
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(self.controller.session()["id"], session)
        self.assertEqual(self.controller.status()["phase"], "STARTING")
        self.assertIs(self.backend.processes[0], self.game)


class ModeCompletenessTests(unittest.TestCase):
    def test_capture_zone_union_must_overlap_runtime_battlefield_coordinates(self):
        config = playable(modes=["campaign"])
        config["bounds"].update(maxX=127, maxZ=127)
        capture = config["mode_data"]["campaign"]["points"][0]
        outside = {
            "minX": 129,
            "maxX": 200,
            "minY": 60,
            "maxY": 70,
            "minZ": 0,
            "maxZ": 10,
        }
        for boxes in (
            [outside],
            [outside, outside | {"minX": -10, "maxX": -1}],
            [outside | {"minX": 200, "maxX": 129}],
            [outside | {"minX": 0, "maxX": 10, "minZ": 129, "maxZ": 200}],
        ):
            capture["zone"] = boxes
            with (
                self.subTest(boxes=boxes),
                self.assertRaisesRegex(ValueError, "does not overlap"),
            ):
                map_package.validate_playable(config)
        capture["zone"] = []
        capture["aabb"] = outside
        with self.assertRaisesRegex(ValueError, "does not overlap"):
            map_package.validate_playable(config)

    def test_capture_zones_allow_edge_overlap_inverted_cells_and_mixed_unions(self):
        config = playable(modes=["room"])
        config["bounds"].update(maxX=127, maxZ=127)
        capture = config["mode_data"]["campaign"]["points"][0]
        box = {"minX": -10, "maxX": 0, "minY": 70, "maxY": 60, "minZ": -10, "maxZ": 0}
        for boxes in (
            [box],
            [box | {"minX": 5, "maxX": -10, "minZ": 5, "maxZ": -10}],
            [box | {"minX": 127, "maxX": 127, "minZ": 127, "maxZ": 127}],
            [box | {"minX": 128, "maxX": 128, "minZ": 128, "maxZ": 128}],
            [box | {"minX": 500, "maxX": 501}, box],
        ):
            capture["zone"] = boxes
            with self.subTest(boxes=boxes):
                map_package.validate_playable(config)
        config["bounds"]["minX"] = 0.5
        config["bounds"]["minZ"] = 0.5
        capture["zone"] = []
        capture["aabb"] = box
        map_package.validate_playable(config)

    def test_mode_failures_follow_actual_spawn_target_and_zone_rules(self):
        config = playable()
        map_package.validate_playable(config)
        mutations = [
            lambda c: c["mode_data"]["campaign"]["bases"].pop("BlueTeam"),
            lambda c: c["mode_data"]["campaign"].update(points=[]),
            lambda c: c["mode_data"]["campaign"]["points"][0].update(
                zone=[{"minX": 1}]
            ),
            lambda c: c["mode_data"]["deathmatch"]["spawn_points"].__setitem__(
                1, {"x": 4.5, "y": 65, "z": 4.5}
            ),
            lambda c: c["mode_data"]["deathmatch"]["spawn_points"][0].update(yaw=181),
            lambda c: c["mode_data"]["training"]["targets"][0].update(armor="diamond"),
            lambda c: c["mode_data"]["training"]["targets"][0].update(
                motion={"type": "strafe", "to_x": 1000, "speed": 1}
            ),
            lambda c: c["mode_data"]["training"]["targets"][0].update(
                motion={"type": "strafe", "to_x": 11, "speed": 0}
            ),
            lambda c: c["mode_data"]["training"]["forceload_chunks"].update(maxX=64),
            lambda c: c["mode_data"]["training"].update(
                zones={"bad": {"minX": 0, "maxX": 128, "minZ": 0, "maxZ": 1}}
            ),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(config)
            mutate(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                map_package.validate_playable(changed)

    def test_optional_lobbies_base_fallback_and_valid_training_strafe(self):
        config = playable()
        training = config["mode_data"]["training"]
        training["targets"][0].update(
            motion={"type": "strafe", "to_x": 11, "speed": 20}, armor="class:assault"
        )
        map_package.validate_playable(config)
        map_package.validate_playable({"id": "legacy", "supported_modes": []})

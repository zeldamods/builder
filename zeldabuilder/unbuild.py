from collections import defaultdict
from joblib import Parallel, delayed
import multiprocessing
from pathlib import Path
import re
import shutil
import subprocess
import sys
import typing
import yaml

import aamp
import aamp.yaml_util
import byml
import byml.yaml_util
import sarc
import wszst_yaz0

import zeldabuilder.file as file

_num_cores = multiprocessing.cpu_count()

_UNHANDLED_CONTENT_PREFIXES = [
    # No tooling, platform-specific.
    "Camera/",
    # No tooling.
    "Effect/",
    # No tooling.
    "ELink2/",
    # No tooling.
    "Env/",
    # Not useful.
    "Font/",
    # Supposed to be autogenerated.
    "Game/",
    # No tooling.
    "Layout/",
    # No tooling, platform-specific.
    "Model/",
    # Doesn't belong in source.
    "Movie/",
    # Supposed to be autogenerated.
    "NavMesh/",
    # Supposed to be autogenerated.
    "Physics/",
    # Platform-specific.
    "Shader/",
    # No tooling.
    "SLink2/",
    # Doesn't belong in source.
    "Sound/",
    # Doesn't belong in source.
    "System/",
    # Supposed to be autogenerated.
    "Terrain/",
    # Doesn't belong in source.
    "UI/",
    # Doesn't belong in source.
    "Voice/",
]

def is_unhandled_content(path: Path):
    s = path.as_posix()
    # Exceptions
    if s == "System/Version.txt" or s == "System/AocVersion.txt":
        return False
    for prefix in _UNHANDLED_CONTENT_PREFIXES:
        if s.startswith(prefix):
            return True
    return False

def is_resource_pack_path(path: Path):
    return path.suffix in ['.sbactorpack', '.sbeventpack', '.bactorpack', '.beventpack', '.pack']

def dump_byml_data(data, stream=None, default_flow_style=None) -> None:
    class Dumper(yaml.CDumper): pass
    byml.yaml_util.add_representers(Dumper)
    return yaml.dump(data, stream=stream, Dumper=Dumper, allow_unicode=True, encoding="utf-8", default_flow_style=default_flow_style)

def dump_byml(data: bytes, stream=None):
    return dump_byml_data(byml.Byml(data).parse(), stream)

def dump_aamp(data: bytes):
    class Dumper(yaml.CDumper): pass
    reader = aamp.Reader(data, track_strings=True)
    aamp_root = reader.parse()
    aamp.yaml_util.register_representers(Dumper)
    Dumper.__aamp_reader = reader
    return yaml.dump(aamp_root, Dumper=Dumper, allow_unicode=True, encoding="utf-8")

_NO_CONVERSION = ("", lambda: "")
def convert_binary_to_text(rel_path: Path, data: bytes) -> typing.Tuple[str, typing.Callable[[], str]]:
    rel_path_s = rel_path.as_posix()

    if rel_path_s.startswith("Actor/AnimationInfo"):
        return _NO_CONVERSION

    # Handled by process_map_units.
    if rel_path.suffix == ".mubin":
        return _NO_CONVERSION
    # Handled by process_actorinfo.
    if rel_path_s == "Actor/ActorInfo.product.byml":
        return _NO_CONVERSION
    # Handled by process_eventinfo.
    if rel_path_s == "Event/EventInfo.product.byml":
        return _NO_CONVERSION
    # Handled by process_questproduct.
    if rel_path_s == "Quest/QuestProduct.bquestpack":
        return _NO_CONVERSION

    if data[0:4] == b'BY\x00\x02' or data[0:4] == b'YB\x02\x00':
        return (".yml", lambda: dump_byml(data))

    if data[0:4] == b'AAMP':
        return (".yml", lambda: dump_aamp(data))

    return _NO_CONVERSION

def change_paths_for_aoc_map_units(path: Path):
    s = path.as_posix()
    s = s.replace("Map/MainField", "Map/AocMainField")
    return Path(s)

def unbuild_resources(src_rom_dir: Path, dest_dir: Path, is_aoc: bool):
    def get_resources(device: file.FileDevice) -> typing.Iterable[Path]:
        for rel_path in device.list_files():
            if is_unhandled_content(rel_path):
                continue
            yield rel_path

    def process_resource(device: file.FileDevice, rel_path: Path) -> None:
        data = device.read_file_and_decomp(rel_path)

        if is_resource_pack_path(rel_path):
            archive = sarc.SARC(data)
            archive_file_device = file.FileDeviceArchive(archive)
            for srp in get_resources(archive_file_device):
                process_resource(archive_file_device, srp)
        else:
            dest_rel_path = file.remove_extension_prefix_char_from_path(rel_path, 's')
            text_ext, text_data_get = convert_binary_to_text(dest_rel_path, data)
            if text_ext:
                dest_rel_path = file.remove_extension_prefix_char_from_path(dest_rel_path, 'b')
                dest_rel_path = dest_rel_path.with_suffix(dest_rel_path.suffix + text_ext)
            dest_rel_path = file.fix_weird_looking_extensions(dest_rel_path)

            if is_aoc:
                dest_rel_path = change_paths_for_aoc_map_units(dest_rel_path)
                dest_rel_path = dest_rel_path.with_suffix(".aoc" + dest_rel_path.suffix)

            dest_path = dest_dir / dest_rel_path
            if dest_path.is_file():
                return

            if data[0:4] == b'SARC':
                archive = sarc.SARC(data)
                archive.extract_to_dir(str(dest_path))
            else:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                with dest_path.open("wb") as f:
                    f.write(text_data_get() if text_ext else data)

    main_device = file.FileDeviceHostDirectory(src_rom_dir)
    batch_size = 1 if is_aoc else "auto"
    Parallel(n_jobs=_num_cores, verbose=10, batch_size=batch_size)(delayed(process_resource)(main_device, rel_path)
        for rel_path in get_resources(main_device))

def remove_unneeded_aoc_suffixes(dest_dir: Path):
    for path in dest_dir.glob("**/*.aoc.*"):
        non_aoc_path = Path(re.sub("\\.aoc\\.(.*)$", ".\\1", path.as_posix()))
        # Only move .aoc.* to the non AoC path if the files are the same.
        if non_aoc_path.is_file():
            if path.stat().st_size != non_aoc_path.stat().st_size:
                continue
            with path.open("rb") as f, non_aoc_path.open("rb") as non_aoc_f:
                if f.read() != non_aoc_f.read():
                    continue
        path.replace(non_aoc_path)

def convert_messages(dest_dir: Path):
    message_dir = dest_dir / "Message"
    subprocess.run(["msyt", "export", "-d", str(message_dir)], check=True)
    for msbt_path in message_dir.glob("**/*.msbt"):
        msbt_path.unlink()

def process_map_units(dest_dir: Path):
    def add_is_static_to_entries(map_unit: dict, is_static: bool) -> None:
        for entry in map_unit["Objs"]:
            entry["IsStatic"] = is_static

    def process_map_unit_unit(unit_dir: Path):
        unit_name = unit_dir.stem
        static_p = unit_dir / f"{unit_name}_Static.mubin"
        dynamic_p = unit_dir / f"{unit_name}_Dynamic.mubin"
        if not static_p.is_file() or not dynamic_p.is_file():
            return

        with static_p.open("rb") as static_f, dynamic_p.open("rb") as dynamic_f:
            static_d = byml.Byml(static_f.read()).parse()
            dynamic_d = byml.Byml(dynamic_f.read()).parse()
            assert isinstance(static_d, dict) and isinstance(dynamic_d, dict)

        add_is_static_to_entries(static_d, is_static=True)
        add_is_static_to_entries(dynamic_d, is_static=False)

        objs = sorted(static_d["Objs"] + dynamic_d["Objs"], key=lambda obj: obj["HashId"])
        rails = sorted(static_d["Rails"] + dynamic_d["Rails"], key=lambda rail: rail["HashId"])
        merged_map_unit: dict = dict()
        for prop in ["LocationPosX", "LocationPosZ", "LocationSize"]:
            if prop in static_d:
                merged_map_unit[prop] = static_d[prop]
        merged_map_unit["Objs"] = objs
        merged_map_unit["Rails"] = rails

        merged_p = unit_dir / f"{unit_name}.muunt.yml"
        with merged_p.open("w") as f:
            dump_byml_data(merged_map_unit, f)

        static_p.unlink()
        dynamic_p.unlink()

    def process_mubin(path: Path):
        dest_path = path.with_suffix(path.suffix + ".yml")
        with path.open("rb") as f, dest_path.open("w") as destf:
            dump_byml(f.read(), destf)
        path.unlink()

    Parallel(n_jobs=_num_cores, verbose=10, batch_size=1)(delayed(process_map_unit_unit)(unit_dir)
        for unit_dir in (dest_dir / "Map").glob("*/*") if unit_dir.is_dir())
    Parallel(n_jobs=_num_cores, verbose=10, batch_size=1)(delayed(process_mubin)(path)
        for path in (dest_dir / "Map").glob("**/*.mubin"))

_ACTOR_META_KEYS = [
    "aabbMin",
    "aabbMax",
    "addColorR",
    "addColorG",
    "addColorB",
    "addColorA",
    "baseScaleX",
    "baseScaleY",
    "baseScaleZ",
    "boundingForTraverse",
    "bugMask",
    "Chemical",
    "cursorOffsetY",
    "farModelCulling",
    "homeArea",
    "locators",
    "lookAtOffsetY",
    "motorcycleEnergy",
    "rigidBodyCenterY",
    "sortKey",
    "terrainTextures",
    "traverseDist",
    "variationMatAnim",
    "variationMatAnimFrame",
]
def process_actorinfo(dest_dir: Path, platform: str, other_platform_actorinfo_path: typing.Optional[Path]):
    # Create the new ActorMeta / DevActorMeta directories (which are extensions).
    dest_devactormeta_dir = dest_dir / "Actor" / "DevActorMeta"
    dest_devactormeta_dir.mkdir(exist_ok=True)
    dest_actormeta_dir = dest_dir / "Actor" / "ActorMeta"
    dest_actormeta_dir.mkdir(exist_ok=True)

    actorinfo_byml = dest_dir / "Actor" / "ActorInfo.product.byml"
    with actorinfo_byml.open("rb") as f:
        actorinfo = byml.Byml(f.read()).parse()
        assert isinstance(actorinfo, dict)

    other_platform = "cafe" if platform == "nx" else "nx"
    other_actorinfo: typing.Optional[dict] = None
    if other_platform_actorinfo_path:
        # A temporary variable is needed to avoid a mypy error...
        tmp = byml.Byml(wszst_yaz0.decompress_file(str(other_platform_actorinfo_path))).parse()
        assert isinstance(tmp, dict)
        other_actorinfo = tmp

    def fill_in_inst_size(i: int, entry: dict) -> None:
        entry["instSizeCafe"] = -1
        entry["instSizeNx"] = -1
        entry[f"instSize{platform.capitalize()}"] = actor["instSize"]
        if other_actorinfo:
            other_actor = other_actorinfo["Actors"][i]
            assert other_actor["name"] == actor["name"]
            entry[f"instSize{other_platform.capitalize()}"] = other_actor["instSize"]
        entry.pop("instSize", None)

    for i, actor in enumerate(actorinfo["Actors"]):
        is_dev_actor = not (dest_dir / "Actor" / "ActorLink" / f"{actor['name']}.yml").is_file()
        actor_meta: typing.Dict[str, typing.Any] = dict()
        if is_dev_actor:
            actor_meta_path = dest_devactormeta_dir / f"{actor['name']}.yml"
            actor_meta = actor.copy()
            fill_in_inst_size(i, actor_meta)
        else:
            actor_meta_path = dest_actormeta_dir / f"{actor['name']}.yml"
            fill_in_inst_size(i, actor_meta)
            for key in _ACTOR_META_KEYS:
                if key in actor:
                    actor_meta[key] = actor[key]

        with actor_meta_path.open("w") as f:
            dump_byml_data(actor_meta, f, default_flow_style=False)

    actorinfo_byml.unlink()

def process_eventinfo(dest_dir: Path):
    byml_path = dest_dir / "Event" / "EventInfo.product.byml"
    with byml_path.open("rb") as f:
        eventinfo = byml.Byml(f.read()).parse()
        assert isinstance(eventinfo, dict)

    for merged_event_name, event in eventinfo.items():
        event_name, entry_name = merged_event_name.split("<")
        entry_name = entry_name[:-1]

        dest_path = dest_dir / "Event" / "EventMeta" / event_name / f"{entry_name}.yml"
        dest_path.parent.mkdir(exist_ok=True, parents=True)
        with dest_path.open("w") as f:
            dump_byml_data(event, f, default_flow_style=False)

    byml_path.unlink()

def process_questproduct(dest_dir: Path):
    byml_path = dest_dir / "Quest" / "QuestProduct.bquestpack"
    with byml_path.open("rb") as f:
        questinfo = byml.Byml(f.read()).parse()
        assert isinstance(questinfo, list)

    for quest in questinfo:
        dest_path = dest_dir / "Quest" / f"{quest['Name']}.quest.yml"
        dest_path.parent.mkdir(exist_ok=True, parents=True)
        with dest_path.open("w") as f:
            dump_byml_data(quest, f, default_flow_style=False)

    byml_path.unlink()

def process_gamedata(dest_dir: Path):
    gamedata_dir = dest_dir / "GameData"
    (gamedata_dir / "ShopGameDataInfo.yml").unlink()
    shutil.rmtree(gamedata_dir / "savedataformat.sarc")

    gamedata_arc_dir = gamedata_dir / "gamedata.sarc"

    flags: typing.DefaultDict[str, typing.List[typing.Any]] = defaultdict(list)
    flag_types: typing.Dict[str, str] = dict()
    for bgdata_path in sorted(gamedata_arc_dir.glob("*.bgdata")):
        series = bgdata_path.stem[:-2]
        with bgdata_path.open("rb") as f:
            gdata = byml.Byml(f.read()).parse()
        assert isinstance(gdata, dict)
        assert len(gdata) == 1
        flag_type = list(gdata.keys())[0]
        flags[series] += gdata[flag_type]
        if series in flag_types:
            assert flag_types[series] == flag_type
        flag_types[series] = flag_type

    for series, merged_flags in flags.items():
        merged_gdata: dict = dict()
        merged_gdata[flag_types[series]] = merged_flags
        dest_path = dest_dir / "GameData" / "Flag" / f"{series}.yml"
        dest_path.parent.mkdir(exist_ok=True, parents=True)
        with dest_path.open("w") as f:
            dump_byml_data(merged_gdata, f)

    shutil.rmtree(gamedata_arc_dir)

def unbuild(src_rom_dir: Path, dest_dir: Path, platform: str, other_platform_actorinfo_path: typing.Optional[Path], aoc_dir: typing.Optional[Path]) -> None:
    unbuild_resources(src_rom_dir, dest_dir, is_aoc=False)
    if aoc_dir:
        unbuild_resources(aoc_dir, dest_dir, is_aoc=True)
        remove_unneeded_aoc_suffixes(dest_dir)
    convert_messages(dest_dir)
    process_map_units(dest_dir)
    process_actorinfo(dest_dir, platform, other_platform_actorinfo_path)
    process_eventinfo(dest_dir)
    process_questproduct(dest_dir)
    process_gamedata(dest_dir)

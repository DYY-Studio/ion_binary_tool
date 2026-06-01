import argparse
import collections
import copy
import os
import posixpath
import re
import zipfile

from .ion import IonAnnotation, IonBLOB, IonStruct, IonSymbol, IS, ion_type
from .ion_binary import IonBinary
from .ion_hard_reader import hard_symbol_table
from .ion_text import IonText
from .utilities import file_write_binary
from .yj_container import (
        CONTAINER_FORMAT_KFX_ATTACHABLE, CONTAINER_FORMAT_KFX_MAIN, CONTAINER_FORMAT_KFX_METADATA,
        YJFragment, YJFragmentList)
from .yj_symbol_catalog import SYSTEM_SYMBOL_TABLE


__license__ = "GPL v3"
__copyright__ = "2026"


DUMP_FILE_RE = re.compile(r"^([^.]+)\.([0-9]+)\.([0-9]+)\.bin$")

MAIN_CONTAINER_TYPES = {259, 260, 538}
METADATA_CONTAINER_TYPES = {258, 419, 490, 585}
RAW_MEDIA_TYPES = {"$417", "$418"}
CONTAINER_FRAGMENT_TYPES = {"$270", "$419", "$593", "$ion_symbol_table"}


class DumpRecord(object):
    def __init__(self, container_num, fid_sid, ftype_sid, filename, data):
        self.container_num = container_num
        self.fid_sid = fid_sid
        self.ftype_sid = ftype_sid
        self.filename = filename
        self.data = data
        self.fid = None
        self.ftype = None
        self.value = None


def iter_dump_file_data(dump_source):
    if os.path.isdir(dump_source):
        for filename in sorted(os.listdir(dump_source)):
            path = os.path.join(dump_source, filename)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    yield filename, filename, f.read()

    elif zipfile.is_zipfile(dump_source):
        with zipfile.ZipFile(dump_source, "r") as zf:
            for info in sorted(zf.infolist(), key=lambda item: item.filename):
                if info.is_dir():
                    continue

                filename = posixpath.basename(info.filename)
                if filename:
                    yield filename, info.filename, zf.read(info)

    else:
        raise ValueError("Dump input must be a directory or zip file: %s" % dump_source)


def load_dump_records(dump_source):
    records = []
    bad_names = []

    for filename, source_name, data in iter_dump_file_data(dump_source):
        match = DUMP_FILE_RE.match(filename)
        if match is None:
            bad_names.append(source_name)
            continue

        container_num, fid_sid, ftype_sid = match.groups()
        records.append(DumpRecord(container_num, int(fid_sid), int(ftype_sid), source_name, data))

    if bad_names:
        raise ValueError("Unexpected dump file names: %s" % ", ".join(bad_names[:20]))

    if not records:
        raise ValueError("No dump files found in %s" % dump_source)

    return records


def decode_records(records, symtab):
    fragments = YJFragmentList()
    raw_records = {}

    for record in records:
        record.ftype = symtab.get_symbol(record.ftype_sid)
        record.fid = None if record.fid_sid == 348 else symtab.get_symbol(record.fid_sid)

        if record.ftype in RAW_MEDIA_TYPES:
            record.value = IonBLOB(record.data)
            raw_records[record.fid] = record
        else:
            record.value = IonBinary(symtab).deserialize_single_value(record.data, import_symbols=True)
            if isinstance(record.value, IonAnnotation):
                if record.value.is_annotation(record.ftype) and record.fid is None:
                    record.fid = record.ftype
                    record.value = record.value.value
                else:
                    record.value = record.value.value

            fragments.append(YJFragment(ftype=record.ftype, fid=record.fid, value=record.value))

    return fragments, raw_records


def collect_symbol_refs(value, numeric_refs, named_refs):
    data_type = ion_type(value)

    if data_type is IonAnnotation:
        for annotation in value.annotations:
            collect_symbol_refs(annotation, numeric_refs, named_refs)
        collect_symbol_refs(value.value, numeric_refs, named_refs)
    elif isinstance(value, IonStruct):
        for key, val in value.items():
            collect_symbol_refs(key, numeric_refs, named_refs)
            collect_symbol_refs(val, numeric_refs, named_refs)
    elif isinstance(value, list):
        for val in value:
            collect_symbol_refs(val, numeric_refs, named_refs)
    elif isinstance(value, IonSymbol):
        symbol = value.tostring()
        if symbol.startswith("$") and symbol[1:].isdigit():
            numeric_refs.add(int(symbol[1:]))
        else:
            named_refs.add(symbol)


def create_symbol_table_fragment(records, fragments, symtab, extra_symbols=None):
    extra_symbols = [] if extra_symbols is None else list(extra_symbols)
    max_sid = max([record.fid_sid for record in records] + [record.ftype_sid for record in records])
    numeric_refs = set()
    named_refs = set()
    for fragment in fragments:
        collect_symbol_refs(fragment, numeric_refs, named_refs)

    if numeric_refs:
        max_sid = max(max_sid, max(numeric_refs))

    system_symbols = set(SYSTEM_SYMBOL_TABLE.symbols)
    local_min_id = len(SYSTEM_SYMBOL_TABLE.symbols) + 1
    local_symbols = []
    seen_symbols = set(system_symbols)

    for sid in range(local_min_id, max_sid + 1):
        symbol = symtab.symbol_of_id.get(sid, "$%d" % sid)
        if symbol in seen_symbols:
            symbol = "$%d" % sid
        local_symbols.append(symbol)
        seen_symbols.add(symbol)

    for symbol in sorted(named_refs) + extra_symbols:
        if symbol not in seen_symbols:
            local_symbols.append(symbol)
            seen_symbols.add(symbol)

    symbol_table_data = IonStruct(
        IS("symbols"), local_symbols,
        IS("max_id"), local_min_id - 1 + len(local_symbols))

    return YJFragment(IonAnnotation([IS("$ion_symbol_table")], symbol_table_data))


def collect_symbol_value_refs(value, refs):
    data_type = ion_type(value)

    if data_type is IonAnnotation:
        collect_symbol_value_refs(value.value, refs)
    elif isinstance(value, IonStruct):
        for val in value.values():
            collect_symbol_value_refs(val, refs)
    elif isinstance(value, list):
        for val in value:
            collect_symbol_value_refs(val, refs)
    elif isinstance(value, IonSymbol):
        refs.add(value.tostring())


def fragment_key(ftype, fid):
    return (str(ftype), fid.tostring() if isinstance(fid, IonSymbol) else fid)


def drop_unreferenced_position_maps(fragments):
    referenced_symbols = set()
    for fragment in fragments:
        if fragment.ftype not in CONTAINER_FRAGMENT_TYPES and fragment.ftype != "$597":
            collect_symbol_value_refs(fragment.value, referenced_symbols)

    filtered = YJFragmentList()
    dropped = []
    for fragment in fragments:
        if (fragment.ftype == "$597" and fragment.fid is not None and
                fragment.fid.tostring() not in referenced_symbols):
            dropped.append(fragment)
            continue

        filtered.append(fragment)

    return filtered, dropped


def get_container_map(fragments):
    entity_map = fragments.get("$419", first=True)
    if entity_map is None or not isinstance(entity_map.value, IonStruct):
        return []

    result = []
    for entry in entity_map.value.get("$252", []):
        container_id = entry.get("$155")
        contains = set(entry.get("$181", []))
        if container_id is not None:
            result.append((container_id, contains))

    return result


def infer_container_format(records):
    type_ids = {record.ftype_sid for record in records}

    if type_ids & MAIN_CONTAINER_TYPES:
        return CONTAINER_FORMAT_KFX_MAIN

    if type_ids & METADATA_CONTAINER_TYPES:
        return CONTAINER_FORMAT_KFX_METADATA

    return CONTAINER_FORMAT_KFX_ATTACHABLE


def assign_container_ids(records, fragments):
    by_container = collections.OrderedDict()
    for record in records:
        by_container.setdefault(record.container_num, []).append(record)

    container_map = get_container_map(fragments)
    assignments = {}
    used_container_ids = set()

    for container_num, container_records in by_container.items():
        fids = {record.fid for record in container_records}
        best_container_id = None
        best_score = -1

        for container_id, contains in container_map:
            if container_id in used_container_ids:
                continue

            score = len(fids & contains)
            if score > best_score:
                best_score = score
                best_container_id = container_id

        if best_container_id is None or best_score == 0:
            best_container_id = "dump-container-%s" % container_num

        assignments[container_num] = best_container_id
        used_container_ids.add(best_container_id)

    return by_container, assignments


def create_container_fragments(records, fragments, omit_keys=None):
    omit_keys = set() if omit_keys is None else set(omit_keys)
    by_container, assignments = assign_container_ids(records, fragments)
    container_fragments = YJFragmentList()

    for container_num, container_records in by_container.items():
        container_records = [
            record for record in container_records
            if fragment_key(record.ftype, record.fid) not in omit_keys]

        if not container_records:
            continue

        container_info = IonStruct(
            IS("$409"), assignments[container_num],
            IS("$412"), 4096,
            IS("$410"), 0,
            IS("$411"), 0,
            IS("$587"), "",
            IS("$588"), "",
            IS("$161"), infer_container_format(container_records),
            IS("version"), 2,
            IS("$181"), [[record.ftype_sid, record.fid_sid] for record in container_records])
        container_fragments.append(YJFragment(ftype="$270", value=container_info))

    return container_fragments


def create_format_capabilities_fragment():
    return YJFragment(ftype="$593", value=[
        IonStruct(IS("$492"), "kfxgen.positionMaps", IS("version"), 2),
        IonStruct(IS("$492"), "kfxgen.pidMapWithOffset", IS("version"), 1),
        IonStruct(IS("$492"), "kfxgen.textBlock", IS("version"), 1),
        ])


def resource_to_raw_map(fragments):
    entity_map = fragments.get("$419", first=True)
    result = {}

    if entity_map is None or not isinstance(entity_map.value, IonStruct):
        return result

    for dep in entity_map.value.get("$253", []):
        entity_id = dep.get("$155")
        mandatory = dep.get("$254", [])
        if isinstance(entity_id, IonSymbol) and entity_id.tostring().startswith("$"):
            if mandatory:
                result[entity_id] = mandatory[0]

    return result


def create_media_fragments(fragments, raw_records):
    media_fragments = YJFragmentList()
    mapped_raw_fids = set()
    media_map = resource_to_raw_map(fragments)

    for fragment in fragments.get_all("$164"):
        location = fragment.value.get("$165") if isinstance(fragment.value, IonStruct) else None
        raw_fid = media_map.get(fragment.fid)
        raw_record = raw_records.get(raw_fid)

        if location and raw_record is not None:
            media_fragments.append(YJFragment(
                ftype=raw_record.ftype,
                fid=IonSymbol(location),
                value=raw_record.value))
            mapped_raw_fids.add(raw_fid)

    for raw_fid, raw_record in raw_records.items():
        if raw_fid not in mapped_raw_fids:
            media_fragments.append(YJFragment(
                ftype=raw_record.ftype,
                fid=raw_record.fid,
                value=raw_record.value))

    return media_fragments


def get_cover_image_id(fragments):
    metadata = fragments.get("$490", first=True)
    if metadata is None or not isinstance(metadata.value, IonStruct):
        return None

    for category in metadata.value.get("$491", []):
        for item in category.get("$258", []):
            if item.get("$492") == "cover_image":
                return item.get("$307")

    return None


def create_cover_alias_fragment(fragments):
    cover_id = get_cover_image_id(fragments)
    if not cover_id:
        return None

    if fragments.get(ftype="$164", fid=cover_id, first=True) is not None:
        return None

    source = fragments.get("$164", first=True)
    if source is None or not isinstance(source.value, IonStruct):
        return None

    value = copy.deepcopy(source.value)
    value[IS("$175")] = IonSymbol(cover_id)
    return YJFragment(ftype="$164", fid=IonSymbol(cover_id), value=value)


def media_filename(fragment):
    filename = fragment.fid.tostring()
    if posixpath.splitext(filename)[1]:
        return filename

    data = bytes(fragment.value)
    if data.startswith(b"\xff\xd8\xff"):
        return filename + "..jpg"
    if data.startswith(b"\x89PNG"):
        return filename + "..png"
    if data.startswith(b"GIF"):
        return filename + "..gif"
    if data.startswith(b"%PDF"):
        return filename + "..pdf"

    return filename


def write_kfx_zip(fragments, outfile):
    with zipfile.ZipFile(outfile, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        book_ion = IonText().serialize_multiple_values(fragments.filtered(omit_resources=True))
        zf.writestr("book.ion", book_ion)

        for ftype in ["$417", "$418"]:
            for fragment in fragments.get_all(ftype):
                zf.writestr(media_filename(fragment), bytes(fragment.value))


def import_dump(dump_source, outfile):
    symtab = hard_symbol_table(use_yj_symbols=True, context=os.path.basename(os.path.abspath(dump_source)))
    records = load_dump_records(dump_source)
    fragments, raw_records = decode_records(records, symtab)
    fragments, dropped_position_maps = drop_unreferenced_position_maps(fragments)
    media_fragments = create_media_fragments(fragments, raw_records)
    cover_alias = create_cover_alias_fragment(fragments)

    extra_symbols = [fragment.fid.tostring() for fragment in media_fragments]
    if cover_alias is not None:
        extra_symbols.append(cover_alias.fid.tostring())

    complete_fragments = YJFragmentList()
    complete_fragments.append(create_symbol_table_fragment(records, fragments, symtab, extra_symbols=extra_symbols))
    complete_fragments.extend(create_container_fragments(
        records, fragments, omit_keys=[fragment_key(fragment.ftype, fragment.fid) for fragment in dropped_position_maps]))
    complete_fragments.append(create_format_capabilities_fragment())
    complete_fragments.extend(fragments)
    if cover_alias is not None:
        complete_fragments.append(cover_alias)
    complete_fragments.extend(media_fragments)

    write_kfx_zip(complete_fragments, outfile)
    return complete_fragments


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import raw dumped KFX fragments into a KFX Input unpack ZIP.")
    parser.add_argument(
        "dump_source", nargs="?", default="dump",
        help="Directory or zip file containing <container>.<id>.<type>.bin files")
    parser.add_argument("outfile", nargs="?", default="dump_import.kfx-zip", help="Output .kfx-zip/.zip filename")
    args = parser.parse_args(argv)

    fragments = import_dump(args.dump_source, args.outfile)
    file_write_binary(args.outfile + ".summary.txt", (
        "Imported %d fragments into %s\n" % (len(fragments), args.outfile)).encode("utf-8"))


if __name__ == "__main__":
    main()

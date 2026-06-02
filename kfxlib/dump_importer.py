import argparse
import collections
import copy
import os
import posixpath
import re
import sys
import zipfile

from .ion import IonAnnotation, IonBLOB, IonStruct, IonSymbol, IS, ion_type
from .ion_binary import IonBinary
from .ion_hard_reader import hard_symbol_table
from .ion_text import IonText
from .utilities import file_write_binary
from .yj_container import (
        CONTAINER_FORMAT_KFX_ATTACHABLE, CONTAINER_FORMAT_KFX_MAIN, CONTAINER_FORMAT_KFX_METADATA,
        YJFragment, YJFragmentList)
from .yj_symbol_catalog import SYSTEM_SYMBOL_TABLE, YJ_SYMBOLS


__license__ = "GPL v3"
__copyright__ = "2026"


DUMP_FILE_RE = re.compile(r"^([^.]+)\.([0-9]+)\.([0-9]+)\.bin$")

MAIN_CONTAINER_TYPES = {259, 260, 538}
METADATA_CONTAINER_TYPES = {258, 419, 490, 585}
RAW_MEDIA_TYPES = {"$417", "$418"}
CONTAINER_FRAGMENT_TYPES = {"$270", "$419", "$593", "$ion_symbol_table"}
YJ_IMPORT_MAX_SID = 827


def safe_yj_import_max_id():
    return max(0, min(len(YJ_SYMBOLS.symbols), YJ_IMPORT_MAX_SID - len(SYSTEM_SYMBOL_TABLE.symbols)))


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


def numeric_symbol_sid(symbol):
    if isinstance(symbol, IonSymbol):
        text = symbol.tostring()
    elif isinstance(symbol, str):
        text = symbol
    else:
        return None

    if text.startswith("$") and text[1:].isdigit():
        return int(text[1:])

    return None


def rename_symbol_refs(value, renamed_symbols):
    data_type = ion_type(value)

    if data_type is IonAnnotation:
        return IonAnnotation(value.annotations, rename_symbol_refs(value.value, renamed_symbols))

    if isinstance(value, IonStruct):
        result = IonStruct()
        for key, val in value.items():
            result[key] = rename_symbol_refs(val, renamed_symbols)
        return result

    if isinstance(value, list):
        return [rename_symbol_refs(val, renamed_symbols) for val in value]

    if isinstance(value, IonSymbol):
        return renamed_symbols.get(value.tostring(), value)

    return value


def rename_local_numeric_fids(records, fragments, raw_records=None):
    renamed_symbols = {}
    raw_records = {} if raw_records is None else raw_records
    raw_fids = {fid.tostring() for fid in raw_records.keys()}

    for fragment in fragments:
        sid = numeric_symbol_sid(fragment.fid)
        if sid is not None and sid > YJ_IMPORT_MAX_SID and fragment.fid.tostring() not in raw_fids:
            renamed_symbols[fragment.fid.tostring()] = IonSymbol("content_%d" % sid)

    value_refs = set()
    for fragment in fragments:
        collect_symbol_value_refs(fragment.value, value_refs)

    for symbol in value_refs:
        sid = numeric_symbol_sid(symbol)
        if sid is not None and sid > YJ_IMPORT_MAX_SID and symbol not in raw_fids:
            renamed_symbols[symbol] = IonSymbol("content_%d" % sid)

    if not renamed_symbols:
        return fragments, renamed_symbols

    for record in records:
        if isinstance(record.fid, IonSymbol):
            record.fid = renamed_symbols.get(record.fid.tostring(), record.fid)

    renamed_fragments = YJFragmentList()
    for fragment in fragments:
        if fragment.is_single():
            fid = None
        else:
            fid = renamed_symbols.get(fragment.fid.tostring(), fragment.fid) if isinstance(fragment.fid, IonSymbol) else fragment.fid
        renamed_fragments.append(YJFragment(
            ftype=fragment.ftype,
            fid=fid,
            value=rename_symbol_refs(fragment.value, renamed_symbols)))

    return renamed_fragments, renamed_symbols


def create_symbol_table_fragment(records, fragments, symtab, extra_symbols=None, renamed_symbols=None):
    extra_symbols = [] if extra_symbols is None else list(extra_symbols)
    renamed_symbols = {} if renamed_symbols is None else renamed_symbols
    max_sid = max([record.fid_sid for record in records] + [record.ftype_sid for record in records])
    numeric_refs = set()
    named_refs = set()
    for fragment in fragments:
        collect_symbol_refs(fragment, numeric_refs, named_refs)

    if numeric_refs:
        max_sid = max(max_sid, max(numeric_refs))

    yj_max_id = safe_yj_import_max_id()
    system_symbols = set(SYSTEM_SYMBOL_TABLE.symbols)
    imported_symbols = set(YJ_SYMBOLS.symbols[:yj_max_id])
    local_min_id = len(SYSTEM_SYMBOL_TABLE.symbols) + yj_max_id + 1
    local_symbols = []
    seen_symbols = system_symbols | imported_symbols

    for sid in range(local_min_id, max_sid + 1):
        symbol = renamed_symbols.get("$%d" % sid, symtab.symbol_of_id.get(sid, "$%d" % sid))
        if isinstance(symbol, IonSymbol):
            symbol = symbol.tostring()
        if symbol in seen_symbols:
            symbol = "$%d" % sid
        local_symbols.append(symbol)
        seen_symbols.add(symbol)

    for symbol in sorted(named_refs) + extra_symbols:
        if symbol not in seen_symbols:
            local_symbols.append(symbol)
            seen_symbols.add(symbol)

    symbol_table_data = IonStruct(
        IS("imports"), [IonStruct(
            IS("name"), "YJ_symbols",
            IS("version"), 10,
            IS("max_id"), yj_max_id)],
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


def spim_has_eid_offset(spim_entries):
    for entry in spim_entries:
        if isinstance(entry, list) and len(entry) > 2 and entry[2]:
            return True
        if isinstance(entry, IonStruct) and entry.get("$143", 0):
            return True

    return False


def has_section_position_id_map(fragments):
    position_id_map = fragments.get("$265", first=True)
    return position_id_map is not None and isinstance(position_id_map.value, IonStruct)


def has_position_maps_capability(fragments):
    return has_section_position_id_map(fragments) or fragments.get("$621", first=True) is not None


def has_position_id_offset(fragments):
    for fragment in fragments.get_all("$609"):
        if isinstance(fragment.value, IonStruct) and spim_has_eid_offset(fragment.value.get("$181", [])):
            return True

    position_id_map = fragments.get("$265", first=True)
    if position_id_map is None:
        return False

    if isinstance(position_id_map.value, list):
        return spim_has_eid_offset(position_id_map.value)

    if isinstance(position_id_map.value, IonStruct):
        for section_map in position_id_map.value.get("$181", []):
            section_name = section_map.get("$174") if isinstance(section_map, IonStruct) else None
            section_spim = fragments.get(ftype="$609", fid=section_name) if section_name is not None else None
            if section_spim is not None and spim_has_eid_offset(section_spim.value.get("$181", [])):
                return True

    return False


def create_format_capabilities_fragment(fragments):
    capabilities = []

    if has_position_maps_capability(fragments):
        capabilities.append(IonStruct(IS("$492"), "kfxgen.positionMaps", IS("version"), 2))

    if has_position_id_offset(fragments):
        capabilities.append(IonStruct(IS("$492"), "kfxgen.pidMapWithOffset", IS("version"), 1))

    if fragments.get("$145", first=True) is not None:
        capabilities.append(IonStruct(IS("$492"), "kfxgen.textBlock", IS("version"), 1))

    return YJFragment(ftype="$593", value=capabilities)


def resource_to_raw_map(fragments, raw_records):
    entity_map = fragments.get("$419", first=True)
    result = {}
    raw_fids = set(raw_records.keys())

    if entity_map is None or not isinstance(entity_map.value, IonStruct):
        return result

    for dep in entity_map.value.get("$253", []):
        entity_id = dep.get("$155")
        mandatory = dep.get("$254", [])
        if isinstance(entity_id, IonSymbol) and entity_id.tostring().startswith("$"):
            for fid in mandatory:
                if fid in raw_fids:
                    result[entity_id] = fid
                    break
        elif isinstance(entity_id, IonSymbol):
            for fid in mandatory:
                if fid in raw_fids:
                    result[entity_id] = fid
                    break

    return result


def media_extension(value):
    data = bytes(value)
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"GIF"):
        return ".gif"
    if data.startswith(b"%PDF"):
        return ".pdf"

    return ""


def resource_media_location(location, media_value):
    if posixpath.splitext(location)[1]:
        return location

    extension = media_extension(media_value)
    return location + extension if extension else location


def create_media_fragments(fragments, raw_records):
    media_fragments = YJFragmentList()
    mapped_raw_fids = set()
    media_map = resource_to_raw_map(fragments, raw_records)

    for fragment in fragments.get_all("$164"):
        location = fragment.value.get("$165") if isinstance(fragment.value, IonStruct) else None
        raw_fid = media_map.get(fragment.fid)
        raw_record = raw_records.get(raw_fid)

        if location and raw_record is not None:
            location = resource_media_location(location, raw_record.value)
            fragment.value[IS("$165")] = location
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


def as_symbol_text(value):
    return value.tostring() if isinstance(value, IonSymbol) else str(value)


def reading_order_section_ids(fragments):
    for ftype in ["$538", "$258"]:
        fragment = fragments.get(ftype, first=True)
        if fragment is None or not isinstance(fragment.value, IonStruct):
            continue

        for reading_order in fragment.value.get("$169", []):
            for section_id in reading_order.get("$170", []):
                yield section_id


def find_first_image_resource_in_value(fragments, value, visited_stories):
    if isinstance(value, IonAnnotation):
        return find_first_image_resource_in_value(fragments, value.value, visited_stories)

    if isinstance(value, list):
        for item in value:
            resource = find_first_image_resource_in_value(fragments, item, visited_stories)
            if resource is not None:
                return resource
        return None

    if not isinstance(value, IonStruct):
        return None

    if value.get("$159") == "$271" and "$175" in value:
        resource = fragments.get(ftype="$164", fid=value["$175"], first=True)
        if resource is not None:
            return resource

    story_id = value.get("$176")
    if story_id is not None and story_id not in visited_stories:
        visited_stories.add(story_id)
        story = fragments.get(ftype="$259", fid=story_id, first=True)
        if story is not None:
            resource = find_first_image_resource_in_value(fragments, story.value, visited_stories)
            if resource is not None:
                return resource

    for item in value.values():
        resource = find_first_image_resource_in_value(fragments, item, visited_stories)
        if resource is not None:
            return resource

    return None


def first_ordered_image_resource(fragments):
    for section_id in reading_order_section_ids(fragments):
        section = fragments.get(ftype="$260", fid=section_id, first=True)
        if section is None:
            continue

        resource = find_first_image_resource_in_value(fragments, section.value, set())
        if resource is not None:
            return resource

    return fragments.get("$164", first=True)


def create_cover_alias_fragment(fragments):
    cover_id = get_cover_image_id(fragments)
    if not cover_id:
        return None

    if fragments.get(ftype="$164", fid=cover_id, first=True) is not None:
        return None

    source = first_ordered_image_resource(fragments)
    if source is None or not isinstance(source.value, IonStruct):
        return None

    value = copy.deepcopy(source.value)
    value[IS("$175")] = IonSymbol(as_symbol_text(cover_id))
    return YJFragment(ftype="$164", fid=IonSymbol(as_symbol_text(cover_id)), value=value)


def media_filename(fragment):
    filename = fragment.fid.tostring()
    if posixpath.splitext(filename)[1]:
        return filename

    extension = media_extension(fragment.value)
    if extension:
        return filename + extension

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
    fragments, renamed_symbols = rename_local_numeric_fids(records, fragments, raw_records)
    media_fragments = create_media_fragments(fragments, raw_records)
    cover_alias = create_cover_alias_fragment(fragments)

    extra_symbols = [fragment.fid.tostring() for fragment in media_fragments]
    if cover_alias is not None:
        extra_symbols.append(cover_alias.fid.tostring())

    complete_fragments = YJFragmentList()
    complete_fragments.append(create_symbol_table_fragment(
        records, fragments, symtab, extra_symbols=extra_symbols, renamed_symbols=renamed_symbols))
    complete_fragments.extend(create_container_fragments(
        records, fragments, omit_keys=[fragment_key(fragment.ftype, fragment.fid) for fragment in dropped_position_maps]))
    complete_fragments.append(create_format_capabilities_fragment(fragments))
    complete_fragments.extend(fragments)
    if cover_alias is not None:
        complete_fragments.append(cover_alias)
    complete_fragments.extend(media_fragments)

    write_kfx_zip(complete_fragments, outfile)
    return complete_fragments


def default_output_filename(dump_source, extension=".kfx-zip"):
    source_path = os.path.abspath(dump_source)

    if os.path.isdir(source_path):
        source_path = source_path.rstrip("\\/")
        output_dir = os.path.dirname(source_path)
        output_base = os.path.basename(source_path)
    else:
        output_dir = os.path.dirname(source_path)
        output_base = os.path.splitext(os.path.basename(source_path))[0]

    if not output_base:
        output_base = "dump_import"

    return os.path.join(output_dir, output_base + extension)


def kfx_zip_filename_for_epub(epub_filename):
    if epub_filename.lower().endswith(".epub"):
        return epub_filename[:-len(".epub")] + ".kfx-zip"

    return epub_filename + ".kfx-zip"


class SilentLog(object):
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def warn(self, msg):
        pass

    def error(self, msg):
        pass

    def exception(self, msg):
        pass


def convert_kfx_zip_to_epub(kfx_zip_filename, epub_filename):
    calibre_modules = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibre-plugin-modules")
    if calibre_modules not in sys.path:
        sys.path.insert(0, calibre_modules)

    from .message_logging import JobLog, set_logger
    from .yj_book import YJ_Book

    job_log = JobLog(SilentLog())
    set_logger(job_log)
    try:
        epub_data = YJ_Book(kfx_zip_filename).convert_to_epub()
    finally:
        set_logger()

    if job_log.errors:
        raise RuntimeError("EPUB conversion reported errors:\n%s" % "\n".join(job_log.errors))

    file_write_binary(epub_filename, epub_data)
    return epub_data, job_log


def remove_intermediate_kfx_zip(filename):
    os.remove(filename)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import raw dumped KFX fragments into a KFX Input unpack ZIP.")
    parser.add_argument(
        "dump_source", nargs="?", default="dump",
        help="Directory or zip file containing <container>.<id>.<type>.bin files")
    parser.add_argument(
        "outfile", nargs="?", default=None,
        help="Output filename. Defaults to the input file/folder name; means EPUB when --epub is used.")
    parser.add_argument(
        "--summary", action="store_true",
        help="Also write a small .summary.txt file next to the output.")
    parser.add_argument(
        "--epub", action="store_true",
        help="Also convert the generated KFX-ZIP to EPUB.")
    parser.add_argument(
        "--keep-kfx-zip", action="store_true",
        help="Keep the intermediate KFX-ZIP when converting to EPUB.")
    args = parser.parse_args(argv)

    if args.epub:
        epub_filename = args.outfile or default_output_filename(args.dump_source, ".epub")
        kfx_zip_filename = kfx_zip_filename_for_epub(epub_filename)
        if os.path.abspath(epub_filename) == os.path.abspath(kfx_zip_filename):
            raise ValueError("EPUB output filename must be different from the intermediate KFX-ZIP filename")
    else:
        epub_filename = None
        kfx_zip_filename = args.outfile or default_output_filename(args.dump_source)

    fragments = import_dump(args.dump_source, kfx_zip_filename)

    media_count = len(fragments.get_all("$417")) + len(fragments.get_all("$418"))
    print("Imported %d fragments from %s" % (len(fragments), os.path.abspath(args.dump_source)))
    print("Media resources: %d" % media_count)
    print("Wrote KFX-ZIP: %s (%d bytes)" % (os.path.abspath(kfx_zip_filename), os.path.getsize(kfx_zip_filename)))

    if args.summary:
        summary_filename = kfx_zip_filename + ".summary.txt"
        file_write_binary(summary_filename, (
            "Imported %d fragments into %s\n" % (len(fragments), kfx_zip_filename)).encode("utf-8"))
        print("Summary: %s" % os.path.abspath(summary_filename))

    if args.epub:
        epub_data, job_log = convert_kfx_zip_to_epub(kfx_zip_filename, epub_filename)
        print("Wrote EPUB: %s (%d bytes)" % (os.path.abspath(epub_filename), len(epub_data)))

        if not args.keep_kfx_zip:
            remove_intermediate_kfx_zip(kfx_zip_filename)
            print("Removed intermediate KFX-ZIP: %s" % os.path.abspath(kfx_zip_filename))

        if job_log.warnings:
            print("EPUB conversion warnings: %d" % len(job_log.warnings))
            for warning in job_log.warnings[:20]:
                print("Warning: %s" % warning)
            if len(job_log.warnings) > 20:
                print("Warning: ... %d more" % (len(job_log.warnings) - 20))


if __name__ == "__main__":
    main()

import argparse
import copy
import os
import sys

from .ion_binary import IonBinary
from .ion_symbol_table import LocalSymbolTable, SymbolTableImport, SymbolTableCatalog, global_catalog
from .ion_text import IonText
from .utilities import file_read_binary, file_write_binary
from .yj_symbol_catalog import YJ_SYMBOLS


__license__ = "GPL v3"
__copyright__ = "2026"


class HardLocalSymbolTable(LocalSymbolTable):
    """
    A permissive symbol table for inspecting Ion streams that are missing or
    have inconsistent local/shared symbol table metadata.
    """

    def __init__(self, initial_import=YJ_SYMBOLS.name, context="", catalog=global_catalog, ignore_max_id=True):
        self.ignore_max_id = ignore_max_id
        LocalSymbolTable.__init__(self, initial_import=initial_import, context=context, ignore_undef=True,
                                  catalog=catalog)

    def create(self, symbol_table_data, yj_local_symbols=False):
        if self.ignore_max_id:
            symbol_table_data = self.without_max_id(symbol_table_data)

        return LocalSymbolTable.create(self, symbol_table_data, yj_local_symbols=yj_local_symbols)

    def import_shared_symbol_table(self, name, version=None, max_id=None):
        symbol_table = self.catalog.get_shared_symbol_table(name, version)
        if symbol_table is not None or name == "$ion":
            return LocalSymbolTable.import_shared_symbol_table(self, name, version=version, max_id=max_id)

        version = version or 1
        max_id = max_id or 0
        self.table_imports.append(SymbolTableImport(name, version, max_id))
        if max_id:
            self.import_symbols([None] * max_id)

        self.local_min_id = len(self.symbols) + 1

    def report(self):
        if self.reported:
            return

        context = ("%s: " % self.context) if self.context else ""

        if self.unexpected_used_symbols:
            from .message_logging import log
            from .utilities import list_symbols

            log.error("%sUnexpected Ion symbols used: %s" % (context, list_symbols(self.unexpected_used_symbols)))

        self.reported = True

    @staticmethod
    def without_max_id(symbol_table_data):
        symbol_table_data = copy.deepcopy(symbol_table_data)

        if "max_id" in symbol_table_data:
            del symbol_table_data["max_id"]

        return symbol_table_data


def hard_symbol_table(use_yj_symbols=True, symbol_catalog_filename=None, ignore_max_id=True, context=""):
    catalog = SymbolTableCatalog(add_global_shared_symbol_tables=True)

    if symbol_catalog_filename is not None:
        catalog_symtab = HardLocalSymbolTable(initial_import=None, catalog=catalog, ignore_max_id=ignore_max_id,
                                             context="symbol catalog")
        IonText(catalog_symtab).deserialize_multiple_values(file_read_binary(symbol_catalog_filename),
                                                            import_symbols=True)

    return HardLocalSymbolTable(
        initial_import=YJ_SYMBOLS.name if use_yj_symbols else None,
        context=context,
        catalog=catalog,
        ignore_max_id=ignore_max_id)


def deserialize_hard_ion(data, symtab=None, import_symbols=True, with_offsets=False):
    if symtab is None:
        symtab = hard_symbol_table()

    if data.startswith(IonBinary.SIGNATURE):
        return IonBinary(symtab).deserialize_multiple_values(data, import_symbols=import_symbols,
                                                            with_offsets=with_offsets)

    if with_offsets:
        raise ValueError("--offsets is only supported for binary Ion input")

    return IonText(symtab).deserialize_multiple_values(data, import_symbols=import_symbols)


def serialize_ion_text(values, human_text=False):
    ion_text = IonText()
    ion_text.escape_unicode = not human_text
    return ion_text.serialize_multiple_values(values)


def hard_read_file(infile, outfile=None, use_yj_symbols=True, symbol_catalog_filename=None,
                   import_symbols=True, ignore_max_id=True, with_offsets=False, human_text=False):
    data = file_read_binary(infile)
    symtab = hard_symbol_table(
        use_yj_symbols=use_yj_symbols,
        symbol_catalog_filename=symbol_catalog_filename,
        ignore_max_id=ignore_max_id,
        context=os.path.basename(infile))

    values = deserialize_hard_ion(data, symtab=symtab, import_symbols=import_symbols, with_offsets=with_offsets)
    out_data = serialize_ion_text(values, human_text=human_text)

    if outfile:
        file_write_binary(outfile, out_data)
    else:
        stdout = getattr(sys.stdout, "buffer", None)
        if stdout is None:
            sys.stdout.write(out_data.decode("utf-8"))
        else:
            stdout.write(out_data)

    return values


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Hard-read Amazon Ion using KFX Input symbol knowledge, keeping unknown SIDs as $id.")
    parser.add_argument("infile", help="Ion binary/text file to read")
    parser.add_argument("outfile", nargs="?", help="Optional Ion text output file")
    parser.add_argument("--catalog", help="Optional Ion text shared symbol catalog")
    parser.add_argument("--no-yj-symbols", action="store_true", help="Do not pre-load the built-in YJ_symbols catalog")
    parser.add_argument("--honor-max-id", action="store_true",
                        help="Keep local symbol table max_id validation fields")
    parser.add_argument("--no-import-symbols", action="store_true", help="Do not process embedded symbol table values")
    parser.add_argument("--offsets", action="store_true", help="Include binary value offsets and lengths in output")
    parser.add_argument("--human-text", "--readable-text", action="store_true",
                        help="Emit printable Unicode characters in text instead of \\uXXXX escapes")
    args = parser.parse_args(argv)

    hard_read_file(
        args.infile,
        outfile=args.outfile,
        use_yj_symbols=not args.no_yj_symbols,
        symbol_catalog_filename=args.catalog,
        import_symbols=not args.no_import_symbols,
        ignore_max_id=not args.honor_max_id,
        with_offsets=args.offsets,
        human_text=args.human_text)


if __name__ == "__main__":
    main()

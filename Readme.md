# Ion Binary Tool

* Read Ion Binary File ignoring `max_id` and `symbol_table`
  * just like `protoc --decode_raw`
* Make KFX-ZIP from KFX Fragments without Container

Base on [John Howell's (jhowell) KFX Input Calibre plugin](https://www.mobileread.com/forums/showthread.php?t=291290)

Great work finished by [Codex](https://github.com/openai/codex)

## Usage
* Ion Hard Read
  ```shell
  python hard_read_ion.py "<Your Ion Binary File>"
  ```
* Dump Import (Fragments Input)
  ```shell
  # The fragments must named as
  # <ContainerID>.<File ID>.<Type ID>.bin
  # Container ID do not affect result
  python import_dump.py "<Fragments Dir / Zip>" (Optional)"<KFX-ZIP Output Path>"
  python import_dump.py --epub "<Fragments Dir / Zip>" (Optional)"<EPUB Output Path>"
  python import_dump.py --epub --keep-kfx-zip "<Fragments Dir / Zip>" "<EPUB Output Path>"
  ```

# License
The same as

[John Howell's (jhowell) KFX Input Calibre plugin](https://www.mobileread.com/forums/showthread.php?t=291290)

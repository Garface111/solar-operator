"""CLI for lossless source compaction. Read-only unless --apply is supplied."""
import argparse
import json
from api.source_compaction import compact_batch, compact_legacy_raw

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit verified migrations; default is read-only")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--after-id", type=int, default=0)
    parser.add_argument("--tenant-id")
    parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, default=30)
    parser.add_argument("--max-source-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    print(json.dumps(compact_batch(**vars(args)), sort_keys=True))


if __name__ == "__main__":
    main()

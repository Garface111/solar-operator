"""Preview/apply bounded removal of old non-billing diagnostics."""
import argparse
import json
from api.data_retention import prune_runtime_diagnostics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Remove only eligible diagnostic rows")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(prune_runtime_diagnostics(apply=args.apply, batch_size=args.batch_size,
                                              max_batches=args.max_batches), indent=2))


if __name__ == "__main__": main()

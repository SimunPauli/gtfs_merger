"""
validate_feeds.py — run MobilityData's official GTFS Validator CLI (canonical
GTFS-spec conformance checker) against every GTFS feed in a directory.

gtfs_kit has no validator of its own (no validators module, no field-range
checks -- see stops.py/miscellany.py), so this wraps the jar at
gtfs-validator/gtfs-validator-7.1.0-cli.jar in the repo root instead. Useful
both on raw source releases (e.g. GTFS_CLEAN_DATA_ROOT/<year>, to catch a bad
release like a stops.txt shipped in a projected CRS before it ever reaches
gtfs_merger) and on merged output.
"""

import json
import subprocess
import sys
from pathlib import Path

import config

DEFAULT_JAR = config.GTFS_VALIDATOR_JAR


def validate_feeds_in_dir(
	directory: Path,
	output_base: Path,
	country_code: str = "dk",
	date: str | None = None,
	jar_path: Path = DEFAULT_JAR,
) -> dict[str, dict]:
	"""
	Run the GTFS Validator CLI on every *.zip feed found under `directory`
	(searched recursively, matching how gtfs_merger itself discovers source
	feeds), writing each feed's report to output_base/<feed-stem>/.

	Returns {feed_name: {"errors": int, "warnings": int, "report": Path}}.
	"""
	directory = Path(directory)
	output_base = Path(output_base)
	output_base.mkdir(parents=True, exist_ok=True)

	feeds = sorted(directory.rglob("*.zip"))
	if not feeds:
		print(f"No GTFS zip files found under {directory}")
		return {}

	results = {}
	for feed_path in feeds:
		feed_output = output_base / feed_path.stem
		feed_output.mkdir(parents=True, exist_ok=True)

		cmd = [
			"java", "-jar", str(jar_path),
			"--input", str(feed_path),
			"--output_base", str(feed_output),
			"--country_code", country_code,
			"--skip_validator_update",
		]
		if date:
			cmd += ["--date", date]

		print(f"\nValidating {feed_path.name} -> {feed_output}")
		subprocess.run(cmd, check=False)

		report_path = feed_output / "report.json"
		errors = warnings = 0
		if report_path.exists():
			report = json.loads(report_path.read_text())
			for notice in report.get("notices", []):
				count = notice.get("totalNotices", 0)
				if notice.get("severity") == "ERROR":
					errors += count
				elif notice.get("severity") == "WARNING":
					warnings += count
		else:
			print(f"  WARNING: no report.json produced for {feed_path.name} -- validator likely crashed")

		results[feed_path.name] = {
			"errors": errors,
			"warnings": warnings,
			"report": report_path,
		}
		print(f"  {feed_path.name}: {errors} error notice(s), {warnings} warning notice(s)")

	print("\nValidation summary:")
	for name, r in results.items():
		flag = "  <-- ERRORS" if r["errors"] else ""
		print(f"  {name}: {r['errors']} errors, {r['warnings']} warnings{flag}")

	return results


if __name__ == "__main__":
	if len(sys.argv) < 3:
		print("Usage: python validate_feeds.py <gtfs_dir> <output_base> [country_code]")
		sys.exit(1)

	country = sys.argv[3] if len(sys.argv) > 3 else "dk"
	validate_feeds_in_dir(Path(sys.argv[1]), Path(sys.argv[2]), country_code=country)

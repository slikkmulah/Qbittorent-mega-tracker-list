from __future__ import annotations

from urllib.parse import urlsplit

import requests


# These feeds publish their highest-confidence public trackers. Broad "all"
# feeds are intentionally excluded because they include down, private, and
# unverified entries that can still answer a shallow connectivity test.
SOURCES = {
    "ngosang best": (
        "https://raw.githubusercontent.com/ngosang/trackerslist/refs/heads/master/"
        "trackers_best.txt"
    ),
    "newTrackon stable": "https://newtrackon.com/api/stable",
    "Trackers.Run stable": "https://trackers.run/s/wp_up_hp_hs_v4_v6.txt",
}

SUPPORTED_SCHEMES = {"http", "https", "udp"}


def main() -> None:
    unique_trackers: set[str] = set()
    session = requests.Session()
    session.headers["User-Agent"] = "Qbittorent-mega-tracker-list/2.0"

    successful_sources = 0
    for name, url in SOURCES.items():
        try:
            print(f"Fetching {name}: {url}")
            response = session.get(url, timeout=20)
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"WARNING: failed to fetch {name}: {exc}")
            continue

        source_trackers = set()
        for line in response.text.splitlines():
            tracker = line.strip()
            if not tracker or tracker.startswith("#"):
                continue
            try:
                parts = urlsplit(tracker)
                _ = parts.port
            except ValueError:
                continue
            if parts.scheme.lower() in SUPPORTED_SCHEMES and parts.hostname:
                source_trackers.add(tracker)

        successful_sources += 1
        unique_trackers.update(source_trackers)
        print(f"Accepted {len(source_trackers)} candidate URLs from {name}")

    if successful_sources == 0:
        raise SystemExit("ERROR: every tracker source failed; refusing to erase the current list")

    sorted_trackers = sorted(unique_trackers)
    if not sorted_trackers:
        raise SystemExit("ERROR: sources returned no usable tracker URLs")

    output_filename = "combined_trackers.txt"
    with open(output_filename, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(sorted_trackers) + "\n")

    print(
        f"Saved {len(sorted_trackers)} unique candidates from "
        f"{successful_sources}/{len(SOURCES)} sources to {output_filename}"
    )


if __name__ == "__main__":
    main()


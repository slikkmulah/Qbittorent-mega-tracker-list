import requests

# Define the sources
urls = [
    "https://raw.githubusercontent.com/ngosang/trackerslist/refs/heads/master/trackers_all.txt",
    "https://newtrackon.com/api/stable",
    "https://trackers.run/s/wp_up_hp_hs_v4_v6.txt",
    "https://newtrackon.com/api/live",
    "https://newtrackon.com/api/all",
    "https://newtrackon.com/api/udp",
    "https://newtrackon.com/api/http"
]

def main():
    unique_trackers = set()

    for url in urls:  # Changed from URLS to urls
        try:
            print(f"Fetching: {url}")
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            
            # Split lines, strip whitespace, and ignore empty lines or comments
            for line in response.text.splitlines():
                cleaned = line.strip()
                if cleaned and not cleaned.startswith("#"):
                    unique_trackers.add(cleaned)
                        
        except requests.exceptions.RequestException as e:
            print(f"Failed to fetch {url}: {e}")

    # Sort the list for clean formatting
    sorted_trackers = sorted(list(unique_trackers))

    # Write the combined results to a local file
    output_filename = "combined_trackers.txt"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted_trackers) + "\n")
        
    print(f"Successfully saved {len(sorted_trackers)} trackers to {output_filename}")

if __name__ == "__main__":
    main()

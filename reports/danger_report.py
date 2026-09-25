import csv
from collections import Counter
import matplotlib.pyplot as plt


with open("data/danger_events.csv", newline="", encoding="utf-8") as file:
    rows = list(csv.DictReader(file))

counts = Counter(row["time"][:10] for row in rows)

for day, count in sorted(counts.items()):
    print(f"{day}: 위험구역 진입 {count}건")

plt.bar(list(counts.keys()), list(counts.values()))
plt.title("Danger zone entries by date")
plt.ylabel("Entry count")
plt.show()
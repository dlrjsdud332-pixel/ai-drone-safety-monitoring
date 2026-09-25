import csv
from collections import Counter
import matplotlib.pyplot as plt


with open("data/fall_events.csv", newline="", encoding="utf-8") as file:
    rows = list(csv.DictReader(file))

counts = Counter(row["time"][:10] for row in rows)

for day, count in sorted(counts.items()):
    print(f"{day}: 넘어짐 의심 {count}건")

plt.bar(list(counts.keys()), list(counts.values()))
plt.title("Possible fall alerts by date")
plt.ylabel("Alert count")
plt.show()
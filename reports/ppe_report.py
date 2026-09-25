import csv
from collections import Counter
import matplotlib.pyplot as plt

with open("data/ppe_events.csv", newline="", encoding="utf-8") as file:
    rows = list(csv.DictReader(file))

counts = Counter(row["missing_item"] for row in rows)  # 미착용 종류별 기록 건수
labels = ["NO_HARDHAT", "NO_SAFETY_VEST"]
values = [counts[label] for label in labels]

for label, count in zip(labels, values):
    print(f"{label}: {count}건")

plt.bar(["No hardhat", "No safety vest"], values)
plt.title("PPE violation records")
plt.ylabel("Record count")
plt.show()
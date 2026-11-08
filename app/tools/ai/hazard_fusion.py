"""Region-based evidence fusion; never creates a FIRE label from a person."""


def intersection(a, b):
    return max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))


def area(a):
    return max(1, (a[2]-a[0])*(a[3]-a[1]))


def iou(a, b):
    n = intersection(a, b)
    return n / (area(a)+area(b)-n)


class HazardFusion:
    """Require two distinct, consecutive frames at the same FIRE region.

    Confirmations expire after two seconds, including stalls/reconnects.
    A strong or previously established person wins an ambiguous overlap.
    """
    def __init__(self):
        self.previous = []
        self.confirmed_fire = []
        self.person_history = {}
        self.accepted_people = set()
        self.pending_people = {}
        self.last_sequence = -1
        self.last_time = None
        self.suppressed = []

    def update(self, sequence, now, fires):
        fresh = (sequence == self.last_sequence + 1 and self.last_time is not None
                 and 0 < now-self.last_time <= 2.0)
        previous = self.previous if fresh else []
        self.confirmed_fire = [box for box in fires if any(iou(box, old) >= .3 for old in previous)]
        self.previous = list(fires)
        if not fresh:
            self.person_history.clear()
            self.accepted_people.clear()
            self.pending_people.clear()
        self.last_sequence, self.last_time = sequence, now

    def filter_people(self, objects, scores):
        kept = dict(objects)
        self.suppressed = []
        current = {}
        pending = {}
        present = {tid for tid,(kind,_) in objects.items() if kind == 0}
        self.accepted_people.intersection_update(present)
        for tid, (kind, box) in objects.items():
            if kind != 0:
                continue
            score = scores.get(tid, 0.)
            prior = self.person_history.get(tid)
            stable = tid in self.accepted_people or bool(prior and prior[1] >= 3 and iou(box, prior[0]) >= .5)
            overlap = any(intersection(box, fire)/area(box) >= .65 and iou(box, fire) >= .25
                          for fire in self.confirmed_fire)
            old = self.pending_people.get(tid)
            count = old[1]+1 if old and iou(box, old[0]) >= .5 else 1
            pending[tid] = (box, count)
            # New weak tracks need three consecutive spatially consistent observations.
            # Existing/strong tracks are retained, and ByteTrack IDs are never rewritten.
            weak_pending = score < .55 and tid not in self.accepted_people and count < 3
            if (overlap and score < .65 and not stable) or weak_pending:
                kept.pop(tid)
                self.suppressed.append(tid)
            elif not overlap:
                self.accepted_people.add(tid)
                current[tid] = (box, prior[1]+1 if prior and iou(box, prior[0]) >= .5 else 1)
            elif prior:
                current[tid] = prior
        self.pending_people = pending
        self.person_history = current
        return kept

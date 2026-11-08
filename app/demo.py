"""Replay two synthetic events through the portfolio's patrol controller."""

from tools.automation.patrol import SimPatrol


def main():
    sim = SimPatrol(clock=lambda: 1.0, wallclock=lambda: 100.0)
    sim.configure([[0, 0, 5], [10, 0, 5]])
    sources = {"cam-1": ("demo-video",)}
    sim.observe(sim.cycle, [], sources, valid_until=5.0)
    sim.start()

    def observe(kind):
        event = {
            "id": kind.lower(),
            "kind": kind,
            "state": "CURRENT",
            "camera_id": "cam-1",
            "source_id": "demo-video",
            "last_seen": 100.1,
            "review": "PENDING",
        }
        sim.observe(sim.cycle, [event], sources, valid_until=5.0)
        print(kind, sim.state, sim.observation["state"])
        event.update(review="APPROVED", reviews=[{"at": 100.2}])
        sim.observe(sim.cycle, [event], sources, valid_until=5.0)

    observe("PERSON")
    sim.request_resume(sim.cycle)
    print("PERSON reviewed", sim.state, sim.observation["state"])
    sim.start()
    print("Operator pressed START", sim.state)

    observe("FIRE")
    try:
        sim.request_resume(sim.cycle)
    except ValueError as error:
        print("FIRE reviewed", sim.state, error)
    else:
        raise RuntimeError("The fire event should block resumption")


if __name__ == "__main__":
    main()

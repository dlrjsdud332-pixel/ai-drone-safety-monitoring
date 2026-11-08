"""Derived index adapters; never write back to incident audit or Flight Adapter."""
from .store import envelope

class ExistingRecordsAdapter:
    def __init__(self, hub): self.hub=hub
    def report(self, report):
        row=report['incident']; context=report.get('flight_context') or {}
        ids=dict(drone_id=context.get('vehicle_id'),mission_id=context.get('mission_id'),incident_id=row['id'])
        # Reference existing evidence/report files; do not duplicate image blobs.
        payload=dict(incident=row,flight_context=context,evidence_frame=report.get('evidence_frame'))
        self.hub.ingest(envelope('incident_report',payload,observed_at=row['last_seen'],**ids))
        self.hub.ingest(envelope('agent_decision',dict(severity=row['severity'],reason=row['reason'],recommendation=row['recommendation']),observed_at=row['first_seen'],**ids))
        for event in row['evidence']:
            self.hub.ingest(envelope('detection_event',event,observed_at=event['occurred_at'],**ids))
        for review in row['reviews']:
            self.hub.ingest(envelope('review',review,observed_at=review['at'],**ids))
    def flight(self, snapshot, plan):
        ids=dict(drone_id=snapshot['vehicle_id'],mission_id=snapshot['mission_id'])
        telemetry=snapshot.get('telemetry')
        if telemetry:
            self.hub.ingest(envelope('telemetry',telemetry,observed_at=telemetry['captured_at'],**ids))
            self.hub.ingest(envelope('mission_state',dict(state=snapshot['state'],session=snapshot['session'],waypoints=[vars(w) for w in plan.waypoints]),observed_at=telemetry['captured_at'],**ids))

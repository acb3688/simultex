export class MessageRecords {
  constructor() {
    this.records = [];
    this.nextId = 1;
  }

  update(candidates, freezeBefore) {
    if (!candidates.length) return this.records;

    let mutableStart = this.records.findIndex((record) => !record.frozen);
    if (mutableStart < 0) mutableStart = this.records.length;

    // The live tail can contain several blocks: the active message plus
    // Codex's status/footer chrome. Its shape may change as the TUI repaints.
    // Remove obsolete live records, but never remove the immutable prefix.
    if (candidates.length < this.records.length && candidates.length >= mutableStart) {
      this.records.splice(candidates.length);
    }

    const sharedLength = Math.min(this.records.length, candidates.length);
    for (let index = mutableStart; index < sharedLength; index += 1) {
      this.assign(this.records[index], candidates[index], false);
    }
    for (let index = this.records.length; index < candidates.length; index += 1) {
      this.append(candidates[index]);
    }

    // `freezeBefore` is the latest semantic message boundary, not simply the
    // final DOM block: terminal status rows can follow the active message.
    const freezeEnd = Math.min(Math.max(freezeBefore, 0), candidates.length);
    for (let index = mutableStart; index < freezeEnd; index += 1) {
      this.assign(this.records[index], candidates[index], true);
    }

    return this.records;
  }

  append(candidate) {
    const record = { key: `message-${this.nextId}` };
    this.nextId += 1;
    this.assign(record, candidate, false);
    this.records.push(record);
  }

  assign(record, candidate, frozen) {
    const key = record.key;
    Object.assign(record, candidate);
    record.key = key;
    record.frozen = frozen;
    record.signature = `${frozen ? "frozen" : "live"}:${candidate.signature}`;
  }
}

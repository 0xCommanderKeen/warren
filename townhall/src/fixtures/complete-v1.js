// Chronicle's complete v1 contract fixture, read straight out of the monorepo.
//
// Townhall used to vendor a copy of this file and check it for drift on a
// schedule, because it lived in its own repo (observatory) and needed a
// cross-repo token to see Chronicle's tree. In the monorepo that distribution
// problem is gone: Chronicle's fixture is three relative directories away, so
// the test reads the source of truth and drift is impossible by construction.
// Do not re-vendor it. Arcadia got the same treatment in warren#217.
//
// `schema_version` discipline is unaffected — deployed clients still negotiate
// versions with whatever server they meet, which is what the parser tests here
// are actually about.
import completeV1 from "../../../chronicle/tests/fixtures/state-contract/complete-v1.json";

export default completeV1;

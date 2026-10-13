// check:api - the committed generated types must equal a fresh generation (no hand edits, no drift)
import { readFileSync, rmSync } from "node:fs";
const [a, b] = process.argv.slice(2);
const same = readFileSync(a, "utf8") === readFileSync(b, "utf8");
rmSync(b);
if (!same) {
  console.error(`${a} is out of date: run \`make openapi\``);
  process.exit(1);
}

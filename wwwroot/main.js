/* SlopBro
 * by throwaway96
 * https://github.com/throwaway96/slopbro
 * Copyright 2026. Licensed under AGPL v3 or later. No warranties.
 */

const child_process = require("child_process");
const fs = require("fs");

function log(...args) {
  const message = args.join(" ");
  console.log(message);
  fs.appendFileSync("/tmp/slopbro.log", message + "\n");
}

function run() {
  log("entered run()");

  if (process.argv.length < 3) {
    log("error: missing script path");
    process.exit(1);
  }

  const scriptPath = process.argv[2];
  const args = process.argv.slice(3);

  log("scriptPath:", scriptPath);
  log("args:", args.join(" "));

  let status = "unknown error";

  try {
    child_process.execFileSync("/bin/sh", [scriptPath].concat(args));

    status = "success";
  } catch (err) {
    status = "error: " + err.message;
  } finally {
    log("result:", status);

    // XXX: Should we bail out or let the "service" continue?
    //process.exit(0);
  }
}

module.exports = { run };

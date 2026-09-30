// SPDX-License-Identifier: Apache-2.0
//! The rules of the shared case corpus (`verify/parity/cases/`, #272), for every Rust runner.
//!
//! Core's runner (`tests/parity_cases.rs`) and the CLI's two tiers (`crates/microvms-cli/tests/
//! parity_cases.rs` and `crates/microvms-cli/src/guards/parity.rs`) include this file
//! by path, so the three read the corpus by one set of rules. The Python and TypeScript runners
//! restate the same rules; `verify/parity/cases/README.md` is the contract all of them follow.
//!
//! A runner plans the corpus for its surface, calls its own entry point for each case it has to
//! run, hands the answer back to [`Run::judge`], and ends with [`Run::finish`], which fails on
//! anything the plan or the judging recorded. Nothing here calls a surface, so one runner can't
//! pass because another did.

// Each runner uses a different part of this file.
#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

pub const SURFACES: [&str; 4] = ["core", "cli", "py", "ts"];

/// The case every runner must load, and every runner that handles its area must run. A
/// runner pointed at the wrong directory, or with a loader that finds nothing, fails here
/// rather than passing over an empty corpus.
pub const SENTINEL: &str = "wrap-dockerfile/sentinel";

/// The CLI's two tiers and the areas each one answers: a spawned `microvm`
/// (`crates/microvms-cli/tests/parity_cases.rs`) and the scripted fakes
/// (`crates/microvms-cli/src/guards/parity.rs`). Each tier owns one list and leaves the other's cases
/// alone. The split is stated once, here, so a tier can't hand an area to the other without the
/// other one planning it and failing on a case it has no handler for.
pub const CLI_PROCESS_AREAS: [&str; 2] = ["cost", "egress"];
pub const CLI_FAKE_AREAS: [&str; 2] = ["image-name", "error"];

const CASE_KEYS: [&str; 6] = [
    "capability",
    "input",
    "expect",
    "ignore",
    "known_drift",
    "skip",
];

/// The repository root: both crates that include this file sit two levels below it.
pub fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(2)
        .expect("the crate sits under the repository root")
        .to_path_buf()
}

pub fn cases_dir() -> PathBuf {
    repo_root().join("verify").join("parity").join("cases")
}

pub fn table_path() -> PathBuf {
    repo_root()
        .join("verify")
        .join("parity")
        .join("capabilities.toml")
}

/// A surface the corpus is known to disagree with, and the issue that fixes it.
#[derive(Clone, Debug)]
pub struct Drift {
    pub issue: String,
    /// Dot paths into the answer. Each one must differ from `expect`; everything else must
    /// match.
    pub keys: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Case {
    /// `<area>/<name>`, without `.json`.
    pub id: String,
    pub area: String,
    pub capability: String,
    pub input: Value,
    pub expect: Value,
    pub ignore: Vec<String>,
    pub known_drift: BTreeMap<String, Drift>,
    pub skip: BTreeMap<String, String>,
}

impl Case {
    /// `input[key]`, which the case must carry.
    pub fn input(&self, key: &str) -> &Value {
        self.input
            .get(key)
            .unwrap_or_else(|| panic!("{}: input has no {key:?}", self.id))
    }

    pub fn input_str(&self, key: &str) -> &str {
        self.input(key)
            .as_str()
            .unwrap_or_else(|| panic!("{}: input.{key} is not a string", self.id))
    }

    pub fn input_u64(&self, key: &str) -> u64 {
        self.input(key)
            .as_u64()
            .unwrap_or_else(|| panic!("{}: input.{key} is not a whole number", self.id))
    }

    pub fn input_f64(&self, key: &str) -> f64 {
        self.input(key)
            .as_f64()
            .unwrap_or_else(|| panic!("{}: input.{key} is not a number", self.id))
    }

    pub fn input_bool(&self, key: &str) -> bool {
        self.input(key)
            .as_bool()
            .unwrap_or_else(|| panic!("{}: input.{key} is not a boolean", self.id))
    }

    pub fn input_strings(&self, key: &str) -> Vec<String> {
        self.input(key)
            .as_array()
            .unwrap_or_else(|| panic!("{}: input.{key} is not an array", self.id))
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .unwrap_or_else(|| panic!("{}: input.{key} holds a non-string", self.id))
                    .to_string()
            })
            .collect()
    }

    /// `input.binary_hex` as bytes.
    pub fn input_binary(&self) -> Vec<u8> {
        let hex = self.input_str("binary_hex");
        assert!(
            hex.len().is_multiple_of(2),
            "{}: binary_hex has an odd length",
            self.id
        );
        (0..hex.len())
            .step_by(2)
            .map(|at| {
                u8::from_str_radix(&hex[at..at + 2], 16)
                    .unwrap_or_else(|_| panic!("{}: binary_hex isn't hex", self.id))
            })
            .collect()
    }
}

/// What the table says about one surface of one capability.
#[derive(Clone, Debug)]
enum Cell {
    Named,
    Exempt {
        reason: String,
        issue: Option<String>,
    },
}

/// The capability table, read only for what the corpus needs: each row's cell per surface.
fn read_table(path: &Path) -> BTreeMap<String, BTreeMap<String, Cell>> {
    let text =
        std::fs::read_to_string(path).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    let table: toml::Table = text
        .parse()
        .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    let rows = table
        .get("capability")
        .and_then(toml::Value::as_array)
        .unwrap_or_else(|| panic!("{} has no [[capability]] rows", path.display()));
    let mut out = BTreeMap::new();
    for row in rows {
        let id = row
            .get("id")
            .and_then(toml::Value::as_str)
            .unwrap_or_else(|| panic!("a [[capability]] row has no id: {row:?}"));
        let mut cells = BTreeMap::new();
        for surface in SURFACES {
            let cell = match row.get(surface) {
                Some(toml::Value::Table(exempt)) => Cell::Exempt {
                    reason: exempt
                        .get("exempt")
                        .and_then(toml::Value::as_str)
                        .unwrap_or_else(|| panic!("{id}/{surface} is a table with no exempt"))
                        .to_string(),
                    issue: exempt
                        .get("issue")
                        .and_then(toml::Value::as_str)
                        .map(str::to_string),
                },
                Some(toml::Value::String(_) | toml::Value::Array(_)) => Cell::Named,
                other => panic!("{id}/{surface} is neither a name nor an exemption: {other:?}"),
            };
            cells.insert(surface.to_string(), cell);
        }
        out.insert(id.to_string(), cells);
    }
    out
}

fn is_number(text: &str) -> bool {
    !text.is_empty() && !text.starts_with('0') && text.bytes().all(|byte| byte.is_ascii_digit())
}

fn is_issue(text: &str) -> bool {
    text.strip_prefix('#').is_some_and(is_number)
}

/// A trace id from `verify/spec/*.symspec.json`, such as `IMAGE-12`.
fn is_trace(text: &str) -> bool {
    text.split_once('-').is_some_and(|(key, number)| {
        !key.is_empty() && key.bytes().all(|byte| byte.is_ascii_uppercase()) && is_number(number)
    })
}

/// Whether a skip's reason ends by naming what holds the gap: `(#N)` or `(TRACE-N)`. Like a
/// marker's issue, only the shape is checked; review holds the reference.
fn names_its_reference(reason: &str) -> bool {
    reason
        .strip_suffix(')')
        .and_then(|rest| rest.rsplit_once('('))
        .is_some_and(|(_, reference)| is_issue(reference) || is_trace(reference))
}

/// Reads one case file, or says what's wrong with it.
fn read_case(path: &Path, area: &str, name: &str) -> Result<Case, String> {
    let id = format!("{area}/{name}");
    let text = std::fs::read_to_string(path).map_err(|error| format!("{id}: {error}"))?;
    let value: Value =
        serde_json::from_str(&text).map_err(|error| format!("{id}: not JSON: {error}"))?;
    let object = value
        .as_object()
        .ok_or_else(|| format!("{id}: a case is a JSON object"))?;
    for key in object.keys() {
        if !CASE_KEYS.contains(&key.as_str()) {
            return Err(format!(
                "{id}: unknown key {key:?} (a case has {CASE_KEYS:?})"
            ));
        }
    }
    let capability = object
        .get("capability")
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{id}: capability must be a string"))?
        .to_string();
    let input = object
        .get("input")
        .filter(|input| input.is_object())
        .ok_or_else(|| format!("{id}: input must be an object"))?
        .clone();
    let expect = object
        .get("expect")
        .filter(|expect| expect.is_object())
        .ok_or_else(|| format!("{id}: expect must be an object"))?
        .clone();
    let ignore = match object.get("ignore") {
        None => Vec::new(),
        Some(Value::Array(paths)) => paths
            .iter()
            .map(|path| {
                path.as_str()
                    .filter(|path| !path.is_empty())
                    .map(str::to_string)
                    .ok_or_else(|| format!("{id}: ignore holds a non-path"))
            })
            .collect::<Result<_, _>>()?,
        Some(_) => return Err(format!("{id}: ignore must be an array of paths")),
    };
    let mut known_drift = BTreeMap::new();
    if let Some(drifts) = object.get("known_drift") {
        let drifts = drifts
            .as_object()
            .ok_or_else(|| format!("{id}: known_drift must be an object"))?;
        for (surface, drift) in drifts {
            if !SURFACES.contains(&surface.as_str()) {
                return Err(format!("{id}: known_drift names no surface {surface:?}"));
            }
            let issue = drift
                .get("issue")
                .and_then(Value::as_str)
                .filter(|issue| is_issue(issue))
                .ok_or_else(|| format!("{id}: known_drift.{surface}.issue must be \"#N\""))?;
            let keys: Vec<String> = drift
                .get("keys")
                .and_then(Value::as_array)
                .filter(|keys| !keys.is_empty())
                .ok_or_else(|| {
                    format!("{id}: known_drift.{surface}.keys must be a non-empty array")
                })?
                .iter()
                .map(|key| {
                    key.as_str()
                        .filter(|key| !key.is_empty())
                        .map(str::to_string)
                        .ok_or_else(|| format!("{id}: known_drift.{surface}.keys holds a non-path"))
                })
                .collect::<Result<_, _>>()?;
            if drift.as_object().is_some_and(|drift| drift.len() != 2) {
                return Err(format!(
                    "{id}: known_drift.{surface} has exactly issue and keys"
                ));
            }
            known_drift.insert(
                surface.clone(),
                Drift {
                    issue: issue.to_string(),
                    keys,
                },
            );
        }
    }
    let mut skip = BTreeMap::new();
    if let Some(skips) = object.get("skip") {
        let skips = skips
            .as_object()
            .ok_or_else(|| format!("{id}: skip must be an object"))?;
        for (surface, reason) in skips {
            if !SURFACES.contains(&surface.as_str()) {
                return Err(format!("{id}: skip names no surface {surface:?}"));
            }
            let reason = reason
                .as_str()
                .filter(|reason| !reason.trim().is_empty())
                .ok_or_else(|| format!("{id}: skip.{surface} needs a reason"))?;
            if !names_its_reference(reason) {
                return Err(format!(
                    "{id}: skip.{surface} ends by naming its issue or trace id, as `(#N)` or \
                     `(IMAGE-12)`"
                ));
            }
            if known_drift.contains_key(surface) {
                return Err(format!(
                    "{id}: {surface} is both skipped and known to drift"
                ));
            }
            skip.insert(surface.clone(), reason.to_string());
        }
    }
    Ok(Case {
        id,
        area: area.to_string(),
        capability,
        input,
        expect,
        ignore,
        known_drift,
        skip,
    })
}

/// Every `<area>/<case>.json` under `dir`, sorted, plus a problem for each file that isn't one.
fn read_cases(dir: &Path) -> (Vec<Case>, Vec<String>) {
    let mut cases = Vec::new();
    let mut problems = Vec::new();
    let Ok(entries) = std::fs::read_dir(dir) else {
        problems.push(format!(
            "the corpus directory {} can't be read",
            dir.display()
        ));
        return (cases, problems);
    };
    let mut areas: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).collect();
    areas.sort();
    for area in areas {
        let area_name = area
            .file_name()
            .map(|name| name.to_string_lossy().to_string())
            .unwrap_or_default();
        if area.is_file() {
            if area_name != "README.md" {
                problems.push(format!(
                    "{}: only README.md may sit beside the area directories",
                    area.display()
                ));
            }
            continue;
        }
        let mut files: Vec<PathBuf> = match std::fs::read_dir(&area) {
            Ok(entries) => entries.flatten().map(|entry| entry.path()).collect(),
            Err(error) => {
                problems.push(format!("{}: {error}", area.display()));
                continue;
            }
        };
        files.sort();
        for file in files {
            let file_name = file
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default();
            match file_name.strip_suffix(".json") {
                Some(name) if file.is_file() && !name.is_empty() => {
                    match read_case(&file, &area_name, name) {
                        Ok(case) => cases.push(case),
                        Err(problem) => problems.push(problem),
                    }
                }
                _ => problems.push(format!(
                    "{}: a case is a .json file directly under its area",
                    file.display()
                )),
            }
        }
    }
    (cases, problems)
}

/// Whether a surface runs a case, and why it doesn't when it doesn't.
#[derive(Clone, Debug)]
pub enum Decision {
    Run,
    Skip(String),
}

/// One runner's pass over the corpus.
pub struct Run {
    surface: &'static str,
    owned: Vec<&'static str>,
    to_run: Vec<Case>,
    failures: Vec<String>,
    ran: BTreeMap<String, usize>,
    judged: BTreeSet<String>,
    sentinel_loaded: bool,
    sentinel_ran: bool,
}

impl Run {
    /// Plans the real corpus for `surface`. `owned` is the areas this runner has handlers for;
    /// `elsewhere` is the areas another runner of the same surface handles (the CLI's two tiers),
    /// which this one leaves alone.
    pub fn plan(surface: &'static str, owned: &[&'static str], elsewhere: &[&'static str]) -> Run {
        Self::plan_in(&cases_dir(), &table_path(), surface, owned, elsewhere)
    }

    pub fn plan_in(
        dir: &Path,
        table: &Path,
        surface: &'static str,
        owned: &[&'static str],
        elsewhere: &[&'static str],
    ) -> Run {
        assert!(SURFACES.contains(&surface), "no surface {surface:?}");
        let table = read_table(table);
        let (cases, mut failures) = read_cases(dir);
        for area in owned {
            if elsewhere.contains(area) {
                failures.push(format!(
                    "area {area:?} is both this {surface} runner's and another's"
                ));
            }
        }
        let mut to_run = Vec::new();
        let mut sentinel_loaded = false;
        for case in cases {
            sentinel_loaded |= case.id == SENTINEL;
            let Some(row) = table.get(&case.capability) else {
                failures.push(format!(
                    "{}: capability {:?} names no row in verify/parity/capabilities.toml",
                    case.id, case.capability
                ));
                continue;
            };
            let decision = match decide(&case, row, surface) {
                Ok(decision) => decision,
                Err(problem) => {
                    failures.push(problem);
                    continue;
                }
            };
            match decision {
                Decision::Skip(reason) => report_skip(surface, &case.id, &reason),
                Decision::Run if owned.contains(&case.area.as_str()) => to_run.push(case),
                Decision::Run if elsewhere.contains(&case.area.as_str()) => {}
                Decision::Run => failures.push(format!(
                    "{}: the {surface} runner has no handler for area {:?}, and the table \
                     doesn't exempt {surface} from {:?}",
                    case.id, case.area, case.capability
                )),
            }
        }
        Run {
            surface,
            owned: owned.to_vec(),
            to_run,
            failures,
            ran: BTreeMap::new(),
            judged: BTreeSet::new(),
            sentinel_loaded,
            sentinel_ran: false,
        }
    }

    /// The cases this runner has to answer, in corpus order.
    pub fn cases(&self) -> Vec<Case> {
        self.to_run.clone()
    }

    /// Compares one answer with the case's `expect`, under its `ignore` and `known_drift`.
    ///
    /// An answer to an error case is `{"error": {"code", "wire_kind", "retryable"}}`, and only
    /// the facets `expect.error` names are compared, so a refusal case can assert its code alone.
    pub fn judge(&mut self, case: &Case, answer: Value) {
        self.judged.insert(case.id.clone());
        *self.ran.entry(case.area.clone()).or_default() += 1;
        self.sentinel_ran |= case.id == SENTINEL;
        let problems = compare(case, self.surface, answer);
        self.failures.extend(problems);
    }

    /// Fails on everything recorded, on an owned area where nothing ran, on a planned case
    /// nobody judged, and on a missing sentinel.
    pub fn finish(mut self) {
        for case in &self.to_run {
            if !self.judged.contains(&case.id) {
                self.failures.push(format!(
                    "{}: planned for {} but never judged",
                    case.id, self.surface
                ));
            }
        }
        for area in &self.owned {
            if self.ran.get(*area).copied().unwrap_or(0) == 0 {
                self.failures.push(format!(
                    "no case ran in area {area:?} on {}: the corpus is empty or unread there",
                    self.surface
                ));
            }
        }
        if !self.sentinel_loaded {
            self.failures
                .push(format!("the sentinel {SENTINEL}.json wasn't loaded"));
        }
        let area = SENTINEL.split('/').next().expect("an area");
        if self.owned.contains(&area) && !self.sentinel_ran {
            self.failures.push(format!(
                "the sentinel {SENTINEL}.json didn't run on {}",
                self.surface
            ));
        }
        assert!(
            self.failures.is_empty(),
            "the {} parity runner failed:\n  {}",
            self.surface,
            self.failures.join("\n  ")
        );
    }
}

fn decide(case: &Case, row: &BTreeMap<String, Cell>, surface: &str) -> Result<Decision, String> {
    let drift = case.known_drift.get(surface);
    match &row[surface] {
        Cell::Exempt { reason, issue } => match drift {
            // The table records the gap and the case measures it: run, and expect the drift.
            Some(drift) if issue.as_deref() == Some(drift.issue.as_str()) => Ok(Decision::Run),
            Some(drift) => Err(format!(
                "{}: known_drift.{surface} names {}, but the table exempts {surface} from {:?} \
                 with issue {issue:?}; a drift on an exempt surface names the issue that closes \
                 the gap",
                case.id, drift.issue, case.capability
            )),
            None if case.skip.contains_key(surface) => Err(format!(
                "{}: skip.{surface} repeats what the table already says",
                case.id
            )),
            None => Ok(Decision::Skip(format!(
                "the table exempts {surface} from {:?}: {reason}",
                case.capability
            ))),
        },
        Cell::Named => Ok(match case.skip.get(surface) {
            Some(reason) => Decision::Skip(reason.clone()),
            None => Decision::Run,
        }),
    }
}

/// Prints a skip's reason. It writes to the process's stderr rather than through `eprintln!`,
/// which libtest captures from a passing test, so the reason shows in a green `cargo test` run
/// the way pytest's `-rs` and node's skip lines show theirs.
fn report_skip(surface: &str, id: &str, reason: &str) {
    use std::io::Write as _;
    let _ = writeln!(
        std::io::stderr(),
        "parity: {id} skipped on {surface}: {reason}"
    );
}

fn compare(case: &Case, surface: &str, answer: Value) -> Vec<String> {
    let mut expect = case.expect.clone();
    let mut actual = answer;
    if let (Some(Value::Object(want)), Some(Value::Object(got))) =
        (expect.get("error").cloned(), actual.get_mut("error"))
    {
        got.retain(|key, _| want.contains_key(key));
    }
    for path in &case.ignore {
        remove(&mut expect, path);
        remove(&mut actual, path);
    }
    let mut problems = Vec::new();
    if let Some(drift) = case.known_drift.get(surface) {
        for key in &drift.keys {
            if same_at(&actual, &expect, key) {
                problems.push(format!(
                    "{}: {surface} now agrees at {key}; remove known_drift ({surface}, {key}, {})",
                    case.id, drift.issue
                ));
            }
        }
        for key in &drift.keys {
            remove(&mut expect, key);
            remove(&mut actual, key);
        }
    }
    if !same(&actual, &expect) {
        problems.push(format!(
            "{}: {surface} answered\n      {actual}\n    expected\n      {expect}",
            case.id
        ));
    }
    problems
}

fn get<'a>(value: &'a Value, path: &str) -> Option<&'a Value> {
    path.split('.')
        .try_fold(value, |value, key| value.as_object()?.get(key))
}

fn remove(value: &mut Value, path: &str) {
    let mut parts: Vec<&str> = path.split('.').collect();
    let last = parts.pop().expect("a non-empty path");
    let mut at = value;
    for key in parts {
        match at.as_object_mut().and_then(|object| object.get_mut(key)) {
            Some(next) => at = next,
            None => return,
        }
    }
    if let Some(object) = at.as_object_mut() {
        object.remove(last);
    }
}

fn same_at(left: &Value, right: &Value, path: &str) -> bool {
    match (get(left, path), get(right, path)) {
        (None, None) => true,
        (Some(left), Some(right)) => same(left, right),
        _ => false,
    }
}

/// JSON equality with numbers compared by value, so `3600` and `3600.0` agree the way they do
/// in Python and JavaScript, while a boolean never equals a number.
fn same(left: &Value, right: &Value) -> bool {
    match (left, right) {
        (Value::Number(left), Value::Number(right)) => left.as_f64() == right.as_f64(),
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len() && left.iter().zip(right).all(|(l, r)| same(l, r))
        }
        (Value::Object(left), Value::Object(right)) => same_objects(left, right),
        _ => left == right,
    }
}

fn same_objects(left: &Map<String, Value>, right: &Map<String, Value>) -> bool {
    left.len() == right.len()
        && left
            .iter()
            .all(|(key, value)| right.get(key).is_some_and(|other| same(value, other)))
}

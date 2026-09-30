// SPDX-License-Identifier: Apache-2.0
//! Tests that hold `microvms-app` to the models in `agentd-model`.
//!
//! The library is empty on purpose. The work is in `tests/`: each test takes the rows a model
//! exposes (every input it enumerates, with the answer its specification gives) and drives the
//! app's own function over them. The app's unit tests restate the same tables by hand, and
//! before this crate nothing checked that a restated table was still the model's.
//!
//! It's a crate of its own because `microvms-app` is published and `agentd-model` isn't, so the
//! app can't take even a dev edge onto the model (#296, decision D18).

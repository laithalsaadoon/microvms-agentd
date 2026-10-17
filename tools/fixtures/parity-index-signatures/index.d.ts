// SPDX-License-Identifier: Apache-2.0
// Serialized by TypeDoc 0.28.20 with TypeScript 5.9.3.

export interface Entry {
  value: string;
}

export interface Table {
  /** Entries indexed by name. */
  readonly [name: string]: Entry;
  readonly [index: number]: Entry;
}

export interface ChangedKey {
  readonly [name: symbol]: Entry;
  readonly [index: number]: Entry;
}

export interface ChangedValue {
  readonly [name: string]: Entry | undefined;
  readonly [index: number]: Entry;
}

export interface Writable {
  [name: string]: Entry;
  readonly [index: number]: Entry;
}

export interface Deleted {
  readonly [index: number]: Entry;
}

export interface Reordered {
  readonly [index: number]: Entry;
  readonly [name: string]: Entry;
}

export declare class TableClass {
  readonly [name: string]: Entry;
}

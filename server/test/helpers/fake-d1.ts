// テスト用: Node.js 内蔵の SQLite で Cloudflare D1 の一部の機能をまねる。
// D1 も SQLite なので、本番と同じ SQL（マイグレーションを含む）をそのまま試せる。

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync, type SQLInputValue } from "node:sqlite";

const MIGRATIONS_DIR = join(import.meta.dirname, "..", "..", "migrations");

function toSqliteValue(value: unknown): SQLInputValue {
  // D1 は undefined を受け付けないので、同じようにエラーにして不具合に気づけるようにする
  if (value === undefined) throw new Error("D1_TYPE_ERROR: undefined は bind できません");
  if (typeof value === "boolean") return value ? 1 : 0;
  if (
    value === null ||
    typeof value === "number" ||
    typeof value === "string" ||
    typeof value === "bigint" ||
    value instanceof Uint8Array
  ) {
    return value;
  }
  throw new Error(`D1_TYPE_ERROR: ${typeof value} は bind できません`);
}

const isQuery = (sql: string) => /^\s*(SELECT|WITH)\b/i.test(sql);

class FakeStatement {
  private readonly db: DatabaseSync;
  readonly sql: string;
  private readonly params: SQLInputValue[];

  constructor(db: DatabaseSync, sql: string, params: SQLInputValue[] = []) {
    this.db = db;
    this.sql = sql;
    this.params = params;
  }

  bind(...values: unknown[]): FakeStatement {
    return new FakeStatement(this.db, this.sql, values.map(toSqliteValue));
  }

  runSync() {
    const info = this.db.prepare(this.sql).run(...this.params);
    return {
      success: true,
      results: [],
      meta: { changes: Number(info.changes), last_row_id: Number(info.lastInsertRowid) },
    };
  }

  allSync() {
    const rows = this.db.prepare(this.sql).all(...this.params).map((row) => ({ ...row }));
    return { success: true, results: rows, meta: { changes: 0 } };
  }

  async run() {
    return this.runSync();
  }

  async all() {
    return this.allSync();
  }

  async first(column?: string) {
    const row = this.db.prepare(this.sql).get(...this.params) as Record<string, unknown> | undefined;
    if (!row) return null;
    return column ? (row[column] ?? null) : { ...row };
  }
}

export interface FakeD1 {
  d1: D1Database;
  sqlite: DatabaseSync;
}

export function createFakeD1(): FakeD1 {
  const sqlite = new DatabaseSync(":memory:");
  for (const file of readdirSync(MIGRATIONS_DIR).filter((name) => name.endsWith(".sql")).sort()) {
    sqlite.exec(readFileSync(join(MIGRATIONS_DIR, file), "utf8"));
  }

  const d1 = {
    prepare: (sql: string) => new FakeStatement(sqlite, sql),
    async batch(statements: FakeStatement[]) {
      // D1 の batch は1つのトランザクションとして実行される
      sqlite.exec("BEGIN");
      try {
        const results = statements.map((statement) =>
          isQuery(statement.sql) ? statement.allSync() : statement.runSync(),
        );
        sqlite.exec("COMMIT");
        return results;
      } catch (error) {
        sqlite.exec("ROLLBACK");
        throw error;
      }
    },
    async exec(sql: string) {
      sqlite.exec(sql);
      return { count: 0, duration: 0 };
    },
  };

  return { d1: d1 as unknown as D1Database, sqlite };
}

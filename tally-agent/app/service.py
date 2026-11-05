"""
The ask pipeline. One request, in order:

  1. check the user may read this company            (authorisation)
  2. load party names for the company                (masking dictionary)
  3. mask the question                               -> model sees placeholders
  4. plan: model or keyword router picks a report    (no data seen yet)
  5. resolve placeholders back to real names         -> database gets exact names
  6. run the fixed, parameterised report under RLS   (or live Tally, if asked)
  7. mask the rows                                    -> model sees placeholders
  8. compute the answer in code; optional model explanation, grounding-checked
  9. render for the user: real names only if their role allows
 10. write the audit row -- if that fails, return nothing
"""
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis

from . import db
from .agent.answer import NARRATE_SYSTEM, evidence_text, format_inr, grounded, narrative_prompt, summarize
from .agent.live import LiveUnavailable, TallyLive
from .agent.llm import LLM, LLMUnavailable
from .agent.planner import Plan, PlanDenied, Planner
from .agent.dates import resolve_period
from .auth import User
from .masking import MaskingEngine, NameIndex, normalize_text
from .reports.registry import REPORTS, ReportError, ReportSpec, run_report

_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Ledgers under these groups (at any depth) are named after people or firms
# and are masked. Bank, cash, tax, stock and P&L ledgers stay readable.
PARTY_GROUPS = frozenset({
    "sundry debtors", "sundry creditors", "capital account", "loans (liability)",
    "secured loans", "unsecured loans", "loans & advances (asset)", "deposits (asset)",
})
STOCK_REPORTS = frozenset({"trial_balance", "balance_sheet", "profit_loss", "profit_loss_summary"})
DISPLAY_ROWS = 100


class Denied(Exception):
    pass


class AuditFailure(Exception):
    pass


@dataclass
class AskResult:
    answer: str
    report_id: str | None = None
    report_title: str | None = None
    params: dict = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    explanation: str | None = None
    data_as_of: str | None = None
    lag_minutes: float | None = None
    live: bool = False
    source: str = "rules"
    warnings: list[str] = field(default_factory=list)
    session_id: str = ""
    latency_ms: int = 0


@dataclass
class CompanyContext:
    company_id: int
    name: str
    names: NameIndex | None
    last_sync_at: dt.datetime | None
    lag_minutes: float | None
    sync_status: str | None
    stock_gap: bool = False


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def _normalise(rows: list[dict]) -> list[dict]:
    return [{k: _jsonable(v) for k, v in r.items()} for r in rows]


class AgentService:
    def __init__(
        self,
        *,
        engine: MaskingEngine,
        planner: Planner,
        llm: LLM | None,
        live: TallyLive | None,
        redis_client: aioredis.Redis | None,
        max_rows: int = 20_000,
        cache_ttl: int = 120,
        narrate: bool = True,
        audit_salt: str = "",
        audit_fail_closed: bool = True,
        stale_warn_minutes: int = 10,
        reference_date: str = "",
    ) -> None:
        self.engine = engine
        self.planner = planner
        self.llm = llm
        self.live = live
        self.redis = redis_client
        self.max_rows = max_rows
        self.cache_ttl = cache_ttl
        self.narrate = narrate
        self.audit_salt = audit_salt
        self.audit_fail_closed = audit_fail_closed
        self.stale_warn_minutes = stale_warn_minutes
        self.reference_date = reference_date
        self._ctx_cache: dict[int, tuple[float, CompanyContext]] = {}

    # ----------------------------------------------------------- helpers
    def today(self) -> dt.date:
        if self.reference_date:
            return dt.date.fromisoformat(self.reference_date)
        return dt.date.today()

    @staticmethod
    def session_for(user: User, session_id: str | None) -> tuple[str, str]:
        sid = session_id if session_id and _SESSION_RE.fullmatch(session_id) else uuid.uuid4().hex
        # Namespaced by user: guessing another user's session id gets you nothing.
        return sid, f"{user.id}:{sid}"

    def _hash(self, text: str) -> str:
        return hashlib.sha256((self.audit_salt + "\x00" + text).encode()).hexdigest()

    def _authorise(self, user: User, company_id: int) -> None:
        if int(company_id) not in user.companies:
            raise Denied("you do not have access to this company")

    async def company_context(self, company_id: int) -> CompanyContext:
        # Freshness first: it is never cached, and it also decides whether the
        # cached name dictionary is still current (a new sync may add parties,
        # who must be masked from their very first question).
        async with db.company_scope([company_id]) as con:
            fresh = await con.fetchrow(
                "SELECT last_sync_at, lag_minutes, last_status FROM tally.v_sync_freshness "
                "WHERE company_id = $1", company_id)
        stamp = fresh["last_sync_at"] if fresh else None

        hit = self._ctx_cache.get(company_id)
        if hit and hit[1].last_sync_at == stamp and time.monotonic() - hit[0] < 300:
            ctx = hit[1]
        else:
            async with db.company_scope([company_id]) as con:
                comp = await con.fetchrow(
                    "SELECT name FROM tally.mst_company WHERE company_id = $1", company_id)
                if comp is None:
                    raise Denied("company not found")
                ledgers = await con.fetch(
                    "SELECT ledger, group_path FROM tally.v_ledger_group "
                    "WHERE company_id = $1", company_id)
                stock_gap = await con.fetchval("SELECT tally.fn_stock_gap($1)", company_id)
                voucher_parties = await con.fetch(
                    "SELECT DISTINCT party_name FROM tally.trn_voucher "
                    "WHERE company_id = $1 AND party_name <> ''", company_id)
            parties = [r["ledger"] for r in ledgers
                       if PARTY_GROUPS.intersection(r["group_path"] or [])]
            parties += [r["party_name"] for r in voucher_parties]
            party_set = set(parties)
            others = [r["ledger"] for r in ledgers if r["ledger"] not in party_set]
            ctx = CompanyContext(company_id, comp["name"],
                                 NameIndex.build(parties, others, company=comp["name"]),
                                 stamp, None, None, bool(stock_gap))
            self._ctx_cache[company_id] = (time.monotonic(), ctx)

        if fresh:
            ctx.last_sync_at = fresh["last_sync_at"]
            ctx.lag_minutes = float(fresh["lag_minutes"]) if fresh["lag_minutes"] is not None else None
            ctx.sync_status = fresh["last_status"]
        return ctx

    async def _fetch(self, spec: ReportSpec, company_id: int, params: dict,
                     sync_stamp: str = "") -> tuple[list[dict], bool]:
        key = None
        if self.redis is not None and not spec.sensitive:
            digest = hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
            # Keyed on the sync time: a new sync makes old cached rows unreachable,
            # so cached figures can never be labelled with a newer "as of".
            key = f"rc:{company_id}:{sync_stamp}:{spec.id}:{digest}"
            cached = await self.redis.get(key)
            if cached:
                data = json.loads(cached)
                return data["rows"], data["truncated"]
        async with db.company_scope([company_id]) as con:
            raw, truncated = await run_report(con, spec, company_id, params, max_rows=self.max_rows)
        rows = _normalise(raw)
        if key is not None:
            await self.redis.set(key, json.dumps({"rows": rows, "truncated": truncated}),
                                 ex=self.cache_ttl)
        return rows, truncated

    async def _audit(self, **fields: Any) -> None:
        fields["params"] = json.dumps(fields.get("params") or {}, default=str)
        ip = fields.get("client_ip")
        if ip is not None:
            try:
                fields["client_ip"] = str(ipaddress.ip_address(ip))
            except ValueError:
                fields["client_ip"] = None   # malformed header must not break auditing
        try:
            await db.audit_insert(**fields)
        except Exception as exc:  # noqa: BLE001 -- any failure to audit is handled the same way
            if self.audit_fail_closed:
                raise AuditFailure("audit log unavailable; refusing to return data") from exc

    # ---------------------------------------------------------- public API
    async def ask(self, user: User, question: str, company_id: int | None,
                  session_id: str | None = None, client_ip: str | None = None) -> AskResult:
        t0 = time.monotonic()
        question = normalize_text(question).strip()
        if company_id is None:
            if not user.companies:
                raise Denied("you do not have access to any company")
            company_id = user.companies[0]
        sid, sess = self.session_for(user, session_id)
        base_audit = dict(user_id=user.audit_id, user_role=user.role, client_ip=client_ip,
                          company_id=int(company_id), question_hash=self._hash(question),
                          llm_model=getattr(self.llm, "model", None))
        try:
            self._authorise(user, company_id)
        except Denied:
            await self._audit(**base_audit, question_masked="[withheld: no company access]",
                              outcome="denied", latency_ms=0)
            raise

        ctx = await self.company_context(int(company_id))
        masked = await self.engine.mask_text(question, sess, names=ctx.names)

        try:
            plan = await self.planner.plan(masked.text, user.role, self.today())
        except PlanDenied as exc:
            await self._audit(**base_audit, question_masked=masked.text, outcome="denied",
                              error_detail=str(exc), latency_ms=int((time.monotonic() - t0) * 1000))
            raise Denied(str(exc)) from exc

        if plan.report_id is None:
            await self._audit(**base_audit, question_masked=masked.text, outcome="no_match",
                              latency_ms=int((time.monotonic() - t0) * 1000))
            answer = await self.engine.redisplay(plan.clarification or "", sess,
                                                 may_unmask=user.may_unmask)
            return AskResult(answer=answer, source=plan.source, warnings=plan.warnings,
                             session_id=sid, latency_ms=int((time.monotonic() - t0) * 1000))

        return await self._execute(user, ctx, REPORTS[plan.report_id], plan, sid, sess,
                                   masked.text, base_audit, t0, narrate=True)

    async def run_direct(self, user: User, report_id: str, company_id: int, params: dict,
                         session_id: str | None = None, client_ip: str | None = None) -> AskResult:
        """No-model path: a form picks the report. Works with the GPU switched off."""
        t0 = time.monotonic()
        sid, sess = self.session_for(user, session_id)
        spec = REPORTS.get(report_id)
        if spec is None:
            raise ReportError(f"unknown report '{report_id}'")
        base_audit = dict(user_id=user.audit_id, user_role=user.role, client_ip=client_ip,
                          company_id=int(company_id), llm_model=None,
                          question_hash=self._hash(f"[direct] {spec.id}"))
        try:
            self._authorise(user, company_id)
            if not spec.allowed_for(user.role):
                raise Denied(f"report '{report_id}' requires role {spec.min_role}")
        except Denied as exc:
            await self._audit(**base_audit, question_masked=f"[direct] {spec.id}",
                              report_id=spec.id, outcome="denied", error_detail=str(exc),
                              latency_ms=int((time.monotonic() - t0) * 1000))
            raise

        ctx = await self.company_context(int(company_id))
        # Tokenise the user's real names so the audit trail stays masked and the
        # same placeholder machinery renders the output.
        tokenised: dict[str, Any] = {}
        for p in spec.params:
            v = params.get(p.name)
            if isinstance(v, str):
                v = normalize_text(v).strip()
            if p.type == "ledger" and isinstance(v, str) and v:
                tokenised[p.name] = await self.engine.tokenize(sess, "PARTY", v)
            elif v not in (None, ""):
                tokenised[p.name] = v
        label = f"[direct] {spec.id} " + json.dumps(tokenised, default=str)
        base_audit["question_hash"] = self._hash(label)
        period = resolve_period("", self.today())
        from .agent.planner import finalize  # local import avoids a cycle at module load
        plan = finalize(spec, tokenised, " ".join(str(v) for v in tokenised.values()), period)
        if plan.report_id is None:
            raise ReportError(plan.clarification or "missing parameters")
        plan.source = "direct"
        return await self._execute(user, ctx, spec, plan, sid, sess, label, base_audit, t0,
                                   narrate=False)

    # ------------------------------------------------------------- execute
    async def _execute(self, user: User, ctx: CompanyContext, spec: ReportSpec, plan: Plan,
                       sid: str, sess: str, masked_question: str, base_audit: dict,
                       t0: float, *, narrate: bool) -> AskResult:
        """Run a plan. Any failure after this point is audited too."""
        try:
            return await self._execute_inner(user, ctx, spec, plan, sid, sess, masked_question,
                                              base_audit, t0, narrate=narrate)
        except (AuditFailure, ReportError):
            raise                       # already audited (or the audit itself failed)
        except Exception as exc:
            try:
                await self._audit(**base_audit, question_masked=masked_question, report_id=spec.id,
                                  params=plan.params, outcome="error",
                                  error_detail=type(exc).__name__,
                                  latency_ms=int((time.monotonic() - t0) * 1000))
            except AuditFailure:
                pass
            raise

    async def _execute_inner(self, user: User, ctx: CompanyContext, spec: ReportSpec, plan: Plan,
                             sid: str, sess: str, masked_question: str, base_audit: dict,
                             t0: float, *, narrate: bool) -> AskResult:
        warnings = list(plan.warnings)
        real_params = await self.engine.unmask_params(plan.params, sess)

        live_used = False
        data_as_of: dt.datetime | None = ctx.last_sync_at
        try:
            rows, truncated = await self._fetch(
                spec, ctx.company_id, real_params,
                ctx.last_sync_at.isoformat() if ctx.last_sync_at else "")
        except ReportError as exc:
            await self._audit(**base_audit, question_masked=masked_question, report_id=spec.id,
                              params=plan.params, outcome="error", error_detail=str(exc),
                              latency_ms=int((time.monotonic() - t0) * 1000))
            raise

        if plan.wants_live and spec.id == "ledger_balance":
            if self.live is not None and self.live.enabled:
                try:
                    closing = await self.live.ledger_closing(ctx.name, real_params["ledger"])
                    if closing is not None:
                        group = rows[0]["group_name"] if rows else ""
                        rows = [{"ledger": real_params["ledger"], "group_name": group,
                                 "closing": str(abs(closing)), "dr_cr": "Dr" if closing < 0 else "Cr"}]
                        live_used = True
                        data_as_of = dt.datetime.now(dt.timezone.utc)
                except LiveUnavailable as exc:
                    warnings.append(f"Live Tally lookup failed ({exc}); showing the synced copy.")
            else:
                warnings.append("Live Tally lookup is not enabled; showing the synced copy.")

        if not live_used:
            if ctx.sync_status == "failed":
                warnings.append("The last Tally sync FAILED. Figures are from the last good sync.")
            if ctx.lag_minutes is not None and ctx.lag_minutes > self.stale_warn_minutes:
                warnings.append(f"Data was last synced {ctx.lag_minutes:.0f} minutes ago.")

        if truncated:
            warnings.append(f"This report has more than {self.max_rows:,} rows: the figures "
                            "cover only the first ones. Narrow the period for a complete answer.")
        if spec.id in STOCK_REPORTS and ctx.stock_gap:
            warnings.append("Closing stock is kept in inventory and was not exported as ledger "
                            "values, so stock is not included in these figures.")

        masked_rows = await self.engine.mask_rows(rows, sess, names=ctx.names)
        summary = summarize(spec, masked_rows, plan.params)

        explanation = None
        if narrate and self.narrate and self.llm is not None and masked_rows:
            try:
                text = await self.llm.chat_text(NARRATE_SYSTEM, narrative_prompt(spec, masked_rows, summary))
                ok, _why = grounded(text, evidence=evidence_text(masked_rows, summary))
                if ok and text:
                    explanation = text[:800]
                else:
                    # Deliberately generic: the reason quotes model output, which may
                    # be invented or injected, and must not reach the user.
                    warnings.append("Model explanation withheld: it did not match the figures. "
                                    "The computed summary above is correct.")
            except LLMUnavailable:
                warnings.append("Model explanation unavailable; computed summary shown.")

        may_unmask = user.may_unmask
        answer = await self.engine.redisplay(summary, sess, may_unmask=may_unmask)
        if explanation:
            explanation = await self.engine.redisplay(explanation, sess, may_unmask=may_unmask)
        display_rows = []
        for r in masked_rows[:DISPLAY_ROWS]:
            display_rows.append({
                k: (await self.engine.redisplay(v, sess, may_unmask=may_unmask)
                    if isinstance(v, str) else v)
                for k, v in r.items()
            })
        display_params = {k: (await self.engine.redisplay(v, sess, may_unmask=may_unmask)
                              if isinstance(v, str) else v) for k, v in plan.params.items()}
        if len(masked_rows) > DISPLAY_ROWS:
            warnings.append(f"Showing the first {DISPLAY_ROWS} of {len(masked_rows)} rows.")

        latency = int((time.monotonic() - t0) * 1000)
        # Audit BEFORE returning: if this fails, the user receives nothing.
        await self._audit(**base_audit, question_masked=masked_question, report_id=spec.id,
                          params=plan.params, row_count=len(rows), unmasked_shown=may_unmask,
                          latency_ms=latency, outcome="ok", data_as_of=data_as_of)

        return AskResult(
            answer=answer, report_id=spec.id, report_title=spec.title, params=display_params,
            columns=list(rows[0].keys()) if rows else [], rows=display_rows, row_count=len(rows),
            explanation=explanation,
            data_as_of=data_as_of.isoformat() if data_as_of else None,
            lag_minutes=None if live_used else ctx.lag_minutes, live=live_used,
            source=plan.source, warnings=warnings, session_id=sid, latency_ms=latency,
        )


__all__ = ["AgentService", "AskResult", "Denied", "AuditFailure", "format_inr"]

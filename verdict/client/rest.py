"""Live Flagsmith Management REST API client behind FlagsmithClient."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from verdict.models import (
    Direction,
    Experiment,
    ExperimentResults,
    Exposure,
    Interval,
    MetricResult,
    MetricRole,
    ProposedChange,
)

logger = logging.getLogger("verdict.client.rest")

_VALID_DIRECTIONS: set[str] = {
    "increase",
    "decrease",
    "do_not_increase",
    "do_not_decrease",
    "not_increase",
    "not_decrease",
}


class RestClient:
    """Read and apply Flagsmith experiments over the Management REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        env_key: str,
        *,
        planned_sample: int = 200,
        simulated: bool = False,
        refresh: bool = False,
        refresh_wait_seconds: float = 1.5,
        timeout: float = 15.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("FLAGSMITH_URL is required for RestClient.")
        if not api_key.strip():
            raise ValueError("FLAGSMITH_API_KEY is required for RestClient.")
        if not env_key.strip():
            raise ValueError("Flagsmith environment key is required for RestClient.")

        self.base_url = base_url.rstrip("/")
        self.env_key = env_key.strip()
        self.planned_sample = max(1, planned_sample)
        self.simulated = simulated
        self.refresh = refresh
        self.refresh_wait_seconds = max(0.0, refresh_wait_seconds)
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Api-Key {api_key.strip()}"},
        )
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._refreshed_exposures: set[str] = set()
        self._refreshed_results: set[str] = set()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _env_experiments_url(self, suffix: str = "") -> str:
        return f"{self.base_url}/api/v1/environments/{self.env_key}/experiments{suffix}"

    def _fetch_detail(self, experiment_id: str) -> dict[str, Any]:
        if experiment_id in self._detail_cache:
            return self._detail_cache[experiment_id]

        target = experiment_id.strip()
        if target.isdigit():
            resp = self._client.get(self._env_experiments_url(f"/{target}/"))
            resp.raise_for_status()
            detail = resp.json()
        else:
            list_resp = self._client.get(self._env_experiments_url("/"))
            list_resp.raise_for_status()
            body = list_resp.json()
            items = body.get("results", body if isinstance(body, list) else [])
            match = next((item for item in items if item.get("name") == target), None)
            if match is None:
                raise ValueError(
                    f"Experiment '{experiment_id}' not found in configured Flagsmith environment."
                )
            numeric_id = str(match["id"])
            detail_resp = self._client.get(self._env_experiments_url(f"/{numeric_id}/"))
            detail_resp.raise_for_status()
            detail = detail_resp.json()

        self._detail_cache[experiment_id] = detail
        self._detail_cache[str(detail["id"])] = detail
        return detail

    def _trigger_refresh(self, numeric_id: str, kind: str, seen: set[str]) -> None:
        if not self.refresh or numeric_id in seen:
            return
        seen.add(numeric_id)
        resp = self._client.post(self._env_experiments_url(f"/{numeric_id}/{kind}/refresh/"))
        if resp.status_code == 429:
            logger.info("Flagsmith %s/refresh rate-limited (429); reading cached snapshot.", kind)
            return
        resp.raise_for_status()
        if self.refresh_wait_seconds > 0:
            time.sleep(self.refresh_wait_seconds)

    @staticmethod
    def _planned_weights_from_detail(detail: dict[str, Any]) -> tuple[dict[str, float], dict[int, str]]:
        """Extract planned split weights and mv_option_id -> exposure arm key mapping."""
        feature = detail.get("feature") or {}
        options = feature.get("multivariate_options") or []
        opt_key_by_id: dict[int, str] = {}
        for opt in options:
            opt_id = int(opt["id"])
            key = str(opt.get("key") or opt.get("string_value") or f"Variant_{opt_id}")
            opt_key_by_id[opt_id] = key

        rollout = detail.get("experiment_rollout") or {}
        mv_allocs = rollout.get("multivariate_feature_state_values") or []
        weights: dict[str, float] = {}
        variant_pct_sum = 0.0
        for alloc in mv_allocs:
            mv_id = int(alloc["multivariate_feature_option"])
            pct = float(alloc.get("percentage_allocation") or 0.0)
            variant_pct_sum += pct
            arm_key = opt_key_by_id.get(mv_id, f"Variant_{mv_id}")
            weights[arm_key] = pct / 100.0

        control_pct = max(0.0, 100.0 - variant_pct_sum)
        ordered: dict[str, float] = {"control": control_pct / 100.0}
        ordered.update(weights)
        return ordered, opt_key_by_id

    def get_experiment(self, experiment_id: str) -> Experiment:
        detail = self._fetch_detail(experiment_id)
        name = str(detail.get("name") or experiment_id)
        feature = detail.get("feature") or {}
        is_sim = self.simulated or ("sim" in name.lower()) or ("fault" in name.lower())
        return Experiment(
            id=str(detail["id"]),
            name=name,
            hypothesis=str(detail.get("hypothesis") or ""),
            status=str(detail.get("status") or "running"),
            feature_name=str(feature.get("name") or "result_view"),
            planned_sample=self.planned_sample,
            simulated=is_sim,
        )

    def get_exposures(self, experiment_id: str) -> Exposure:
        detail = self._fetch_detail(experiment_id)
        numeric_id = str(detail["id"])
        self._trigger_refresh(numeric_id, "exposures", self._refreshed_exposures)

        resp = self._client.get(self._env_experiments_url(f"/{numeric_id}/exposures/"))
        resp.raise_for_status()
        exp_obj = (resp.json() or {}).get("exposures") or {}
        payload = exp_obj.get("payload") or {}
        points = (payload.get("timeseries") or {}).get("points") or []

        planned_weights, _ = self._planned_weights_from_detail(detail)
        counts: dict[str, int] = {arm: 0 for arm in planned_weights}
        for pt in points:
            for arm, cnt in (pt.get("new_identities") or {}).items():
                counts[arm] = counts.get(arm, 0) + int(cnt)

        return Exposure(
            as_of=exp_obj.get("as_of"),
            planned_weights=planned_weights,
            counts=counts,
            excluded_identities=int(payload.get("excluded_identities") or 0),
        )

    def get_results(self, experiment_id: str) -> ExperimentResults:
        detail = self._fetch_detail(experiment_id)
        numeric_id = str(detail["id"])
        self._trigger_refresh(numeric_id, "results", self._refreshed_results)

        resp = self._client.get(self._env_experiments_url(f"/{numeric_id}/results/"))
        resp.raise_for_status()
        res_obj = (resp.json() or {}).get("results") or {}
        payload = res_obj.get("payload") or {}
        srm_raw = payload.get("srm_p_value")
        srm_p_value = float(srm_raw) if srm_raw is not None else None

        planned_weights, _ = self._planned_weights_from_detail(detail)
        variant_arms = [a for a in planned_weights if a != "control"]
        default_variant_arm = variant_arms[0] if variant_arms else "verdict"

        attached_metrics = detail.get("metrics") or []
        meta_by_id: dict[int, tuple[int, dict[str, Any]]] = {}
        for idx, m_meta in enumerate(attached_metrics):
            m_id = int(m_meta.get("metric") or m_meta.get("id"))
            meta_by_id[m_id] = (idx, m_meta)

        metric_results: list[MetricResult] = []
        raw_metrics = payload.get("metrics") or []
        seen_metric_ids: set[int] = set()

        for raw_m in raw_metrics:
            m_id = int(raw_m["metric_id"])
            seen_metric_ids.add(m_id)
            idx, meta = meta_by_id.get(m_id, (len(metric_results), {}))
            metric_results.append(
                self._parse_metric_entry(idx, meta, raw_m, default_variant_arm)
            )

        # Include attached metrics not yet populated in cached results payload
        for m_id, (idx, meta) in meta_by_id.items():
            if m_id not in seen_metric_ids:
                metric_results.append(
                    self._parse_metric_entry(idx, meta, {}, default_variant_arm)
                )

        return ExperimentResults(
            as_of=res_obj.get("as_of"),
            srm_p_value=srm_p_value,
            metrics=metric_results,
        )

    @staticmethod
    def _parse_metric_entry(
        idx: int,
        meta: dict[str, Any],
        raw_m: dict[str, Any],
        default_variant_arm: str,
    ) -> MetricResult:
        raw_dir = str(meta.get("expected_direction") or meta.get("direction") or "increase")
        direction: Direction = raw_dir if raw_dir in _VALID_DIRECTIONS else "increase"  # type: ignore[assignment]
        if idx == 0:
            role: MetricRole = "primary"
        elif idx == 1 or direction in (
            "not_increase",
            "not_decrease",
            "do_not_increase",
            "do_not_decrease",
        ):
            role = "guardrail"
        else:
            role = "secondary"

        variants = raw_m.get("variants") or {}
        ctrl_stats = variants.get("control") or {}
        var_arm = next((k for k in variants if k != "control"), default_variant_arm)
        var_stats = variants.get(var_arm) or {}

        inf_map = raw_m.get("inference") or {}
        inf = inf_map.get(var_arm)
        interval: Interval | None = None
        relative_lift: float | None = None
        win_prob: float | None = None
        if isinstance(inf, dict) and inf.get("ci_low") is not None and inf.get("ci_high") is not None:
            interval = Interval(low=float(inf["ci_low"]), high=float(inf["ci_high"]))
            if inf.get("lift") is not None:
                relative_lift = float(inf["lift"])
            if inf.get("chance_to_win") is not None:
                win_prob = float(inf["chance_to_win"])

        return MetricResult(
            name=str(meta.get("metric_name") or f"metric_{raw_m.get('metric_id', idx)}"),
            role=role,
            expected_direction=direction,
            control_arm="control",
            variant_arm=var_arm,
            control_n=int(ctrl_stats.get("n") or 0),
            control_events=round(float(ctrl_stats.get("sum") or 0.0)),
            variant_n=int(var_stats.get("n") or 0),
            variant_events=round(float(var_stats.get("sum") or 0.0)),
            relative_lift=relative_lift,
            interval=interval,
            win_probability=win_prob,
        )

    def propose_rollout(self, experiment_id: str, arm: str) -> ProposedChange:
        detail = self._fetch_detail(experiment_id)
        numeric_id = str(detail["id"])
        feature = detail.get("feature") or {}
        feature_name = str(feature.get("name") or "result_view")
        initial_value = str(feature.get("initial_value") or "")

        if arm in ("control", initial_value):
            payload: dict[str, Any] = {"rollout_decision": "control"}
            desc = (
                f"Complete experiment {numeric_id} ('{detail.get('name')}') and roll out "
                f"control ('{initial_value or 'control'}') to 100% on '{feature_name}'."
            )
        else:
            matched_opt = None
            for opt in feature.get("multivariate_options") or []:
                opt_key = str(opt.get("key") or "")
                opt_val = str(opt.get("string_value") or "")
                if arm in (opt_key, opt_val):
                    matched_opt = opt
                    break
            if matched_opt is None:
                raise ValueError(
                    f"Arm '{arm}' does not match control or any multivariate option on '{feature_name}'."
                )
            opt_id = int(matched_opt["id"])
            payload = {"rollout_decision": "variant", "rollout_variant": opt_id}
            desc = (
                f"Complete experiment {numeric_id} ('{detail.get('name')}') and roll out "
                f"variant '{arm}' (option id={opt_id}) to 100% on '{feature_name}'."
            )

        return ProposedChange(
            experiment_id=numeric_id,
            feature_name=feature_name,
            winning_arm=arm,
            description=desc,
            payload=payload,
        )

    def apply(self, change: ProposedChange) -> dict[str, Any]:
        resp = self._client.post(
            self._env_experiments_url(f"/{change.experiment_id}/complete/"),
            json=change.payload,
        )
        resp.raise_for_status()
        data = resp.json()
        self._detail_cache.pop(change.experiment_id, None)
        return {
            "id": data.get("id"),
            "name": data.get("name"),
            "status": data.get("status"),
            "rollout_decision": data.get("rollout_decision"),
            "rollout_variant": data.get("rollout_variant"),
            "ended_at": data.get("ended_at"),
        }

    def get_environment_feature_state(self, feature_id: int) -> dict[str, Any]:
        """Fetch live environment default FeatureState (feature_segment=null) for a feature."""
        resp = self._client.get(
            f"{self.base_url}/api/v1/environments/{self.env_key}/featurestates/",
            params={"feature": feature_id},
        )
        resp.raise_for_status()
        body = resp.json()
        items = body.get("results", body if isinstance(body, list) else [])
        default_state = next(
            (s for s in items if s.get("feature") == feature_id and s.get("feature_segment") is None),
            None,
        )
        if default_state is None:
            raise ValueError(f"Default featurestate for feature {feature_id} not found.")
        return default_state

    def propose_rollback(self, experiment_id: str) -> ProposedChange:
        """Build a rollback payload resetting environment default variants to 0% (100% control)."""
        detail = self._fetch_detail(experiment_id)
        numeric_id = str(detail["id"])
        feature = detail.get("feature") or {}
        feature_id = int(feature["id"])
        feature_name = str(feature.get("name") or "result_view")
        initial_value = str(feature.get("initial_value") or "control")

        state = self.get_environment_feature_state(feature_id)
        mv_reset = [
            {
                "id": int(mv["id"]),
                "multivariate_feature_option": int(mv["multivariate_feature_option"]),
                "percentage_allocation": 0.0,
            }
            for mv in (state.get("multivariate_feature_state_values") or [])
        ]
        payload = {
            "featurestate_id": int(state["id"]),
            "feature": feature_id,
            "environment": int(state["environment"]),
            "enabled": True,
            "multivariate_feature_state_values": mv_reset,
        }
        return ProposedChange(
            experiment_id=numeric_id,
            feature_name=feature_name,
            winning_arm=initial_value,
            description=(
                f"Roll back '{feature_name}' (featurestate id={state['id']}) to 100% "
                f"control ('{initial_value}') after experiment {numeric_id} ('{detail.get('name')}')."
            ),
            payload=payload,
        )

    def rollback(self, change: ProposedChange) -> dict[str, Any]:
        """Execute a proposed feature-state rollback via Flagsmith Management API."""
        fs_id = int(change.payload["featurestate_id"])
        patch_body = {
            "feature": change.payload["feature"],
            "environment": change.payload["environment"],
            "enabled": change.payload.get("enabled", True),
            "multivariate_feature_state_values": change.payload[
                "multivariate_feature_state_values"
            ],
        }
        resp = self._client.patch(
            f"{self.base_url}/api/v1/environments/{self.env_key}/featurestates/{fs_id}/",
            json=patch_body,
        )
        resp.raise_for_status()
        data = resp.json()
        self._detail_cache.pop(change.experiment_id, None)
        return {
            "featurestate_id": data.get("id"),
            "feature": data.get("feature"),
            "enabled": data.get("enabled"),
            "feature_state_value": data.get("feature_state_value"),
            "multivariate_feature_state_values": data.get("multivariate_feature_state_values"),
        }

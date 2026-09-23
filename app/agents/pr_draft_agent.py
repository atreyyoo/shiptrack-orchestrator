"""The Draft PR Agent — assembles the submit-draft-PR payload from a
completed intake (app/tools/pr_intake_tool.py) once every mandatory field is
filled. Invoked the same way EscalationAgent is invoked once the ticket
intake gate says "ready" — see app/main.py.

Deliberately has NO LLM step, unlike every other agent in this app. Every
value here is either a grounded master-data code (job/warehouse/material/WBS
— resolved by pr_master_data_tools.py, never guessed), a reference-table
enumeration (pr_category/planning_category/supply_at — also DB-resolved), a
computed number (qty * rate), or a fixed constant loaded from the
pr_constants table (see `constants` below). A PR payload is exact,
machine-typed data that an external ERP will act on — unlike EscalationAgent,
which is free to draft ticket prose, there is nothing left here for an LLM to
add, and a hallucinated field would silently submit a wrong PR.

`constants` values are still PLACEHOLDERS — carried over verbatim from the
comments on the example payload you were given ("constants: hpR_PR_Type_Code
1, _Detail_Code 27, _Category_Code 45, _Planning_Category_Type_Code 14,
_Material_Supply_Type_Code 44") — including one guess (hpR_Detail_Code, for
the "27") at which field name that shorthand referred to. They live in the
pr_constants table now (db/02_seed.sql) rather than in this file, so fixing
one is a DB UPDATE, not a code change — but the values themselves still need
confirming against your ERP's real reference-code tables before this goes
near production.

Vendor selection (app/tools/pr_intake_tool.py's "vendor" field, between
Material and WBS) feeds in here too: when a vendor was actually chosen,
`dpR_Suggested_Rate`/`dpR_Value` are computed from THAT vendor's price, not
the material's generic catalog suggested_rate — the whole point of picking a
vendor is that the PR reflects what they actually charge. `fields["vendor"]`
is None when the material had no vendor pricing on file to choose from
(app/main.py auto-skips the question in that case), and build_payload falls
back to the catalog rate exactly as it did before this field existed. There
is no ERP field in the example payload for vendor identity, so rather than
invent one (a hallucinated field name is exactly the risk this class exists
to avoid), the chosen vendor is appended to hpR_Remarks — free text that
already exists — visible on the PR without guessing at the real schema.
"""


class DraftPRAgent:
    def build_payload(self, fields: dict, constants: dict, *, remarks: str | None = None) -> dict:
        """`fields` is the fully-resolved intake state (see
        pr_intake_tool.FIELD_ORDER — every key must be present, "vendor"
        included though its value may be None). `constants` is
        app.tools.pr_master_data_tools.get_pr_constants()'s {key: value}
        dict — the caller (app/main.py) does that one DB read once, so this
        function itself stays a pure computation with no I/O of its own."""
        job = fields["job"]
        warehouse = fields["warehouse"]
        pr_category = fields["pr_category"]
        planning_category = fields["planning_category"]
        supply_at = fields["supply_at"]
        material = fields["material"]
        vendor = fields.get("vendor")
        wbs = fields["wbs"]

        qty = float(fields["quantity"])
        rate = float(vendor["price"]) if vendor else float(material["suggested_rate"])
        value = round(qty * rate, 2)

        vendor_note = f"Vendor: {vendor['vendor_name']} ({vendor['vendor_code']})" if vendor else None
        full_remarks = " | ".join(part for part in (remarks, vendor_note) if part)

        return {
            "a_AccessFlag": constants["access_flag_insert"],
            "a_DOPTCode": constants["dopt_code_create"],
            "S_Finyear": constants["finyear"],
            "S_Comp_abb_code": constants["comp_abb_code"],
            "S_JobSl": job["job_serial"],
            "jsonHeader": {
                "hpR_Job_Code": job["job_code"],
                "hpR_Warehouse_Code": warehouse["warehouse_code"],
                "hpR_PR_Category_Detail_Code": pr_category["code"],
                "hpR_Planning_Category_Type_Detail_Code": planning_category["code"],
                "hpR_Material_Supply_Detail_Code": supply_at["code"],
                "hpR_Own_Premises": supply_at["own_premises"],
                "hpR_Remarks": full_remarks,
                "hpR_PR_Value": f"{value:.2f}",
                "hpR_DT_Code": int(constants["dt_code"]),
                "hpR_DS_Code": int(constants["ds_code"]),
                "hpR_PR_Type_Code": int(constants["pr_type_code"]),
                "hpR_Detail_Code": int(constants["detail_code"]),
                "hpR_PR_Category_Code": int(constants["pr_category_code_group"]),
                "hpR_Planning_Category_Type_Code": int(constants["planning_category_type_code_group"]),
                "hpR_Material_Supply_Type_Code": int(constants["material_supply_type_code_group"]),
                "hpR_PO_Initiated_From": constants["po_initiated_from"],
            },
            "jsonDetails": [
                {
                    "dpR_Material_Code": material["material_code"],
                    "dpR_Cost_Package_Code": wbs["default_cost_package_code"],
                    "dpR_WBS_Code": wbs["wbs_code"],
                    "dpR_Qty": qty,
                    "dpR_UOM_Code": material["default_uom_code"],
                    "dpR_Suggested_Rate": rate,
                    "dpR_Value": value,
                    "dpR_DS_CODE": int(constants["ds_code"]),
                    "dpR_DT_Code": int(constants["dt_code"]),
                    "dpR_Action_Flag": constants["action_flag_insert"],
                }
            ],
            # Unknown field list (dprS_*) — see README's "Extending this"
            # section. Sent empty until that shape is provided.
            "jsonScheduleBreakups": [],
        }

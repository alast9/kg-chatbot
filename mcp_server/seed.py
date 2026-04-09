"""
Generate mock cost-analytics CSV data into mcp_server/data/.
Run once:  python mcp_server/seed.py
"""
import csv, os, random
from datetime import datetime, timedelta

random.seed(42)
OUT = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(OUT, exist_ok=True)

# ── Reference tables ──────────────────────────────────────────────────────────

LOBS = [
    (1, "Retail Banking"),
    (2, "Investment Banking"),
    (3, "Risk & Compliance"),
    (4, "Technology & Operations"),
    (5, "Finance & Accounting"),
]

COST_CENTERS = [
    (101, "Mortgage",              1),
    (102, "Personal Loans",        1),
    (103, "Deposits & Savings",    1),
    (201, "Equity Trading",        2),
    (202, "Fixed Income",          2),
    (203, "Structured Products",   2),
    (301, "Credit Risk",           3),
    (302, "Market Risk",           3),
    (303, "AML & Compliance",      3),
    (401, "Cloud Infrastructure",  4),
    (402, "Data Engineering",      4),
    (403, "Cybersecurity",         4),
    (501, "Financial Reporting",   5),
    (502, "Treasury",              5),
    (503, "Tax & Audit",           5),
]

APPLICATIONS = [
    (1001, "LoanOrigination",       101),
    (1002, "MortgagePOS",           101),
    (1003, "AppraisalMgmt",         101),
    (1004, "PersonalLoanApp",       102),
    (1005, "CreditScoring",         102),
    (1006, "CollectionsMgmt",       102),
    (1007, "DepositCore",           103),
    (1008, "SavingsPortal",         103),
    (1009, "InterestCalc",          103),
    (2001, "EquityOMS",             201),
    (2002, "RiskEngine",            201),
    (2003, "TradeBlotter",          201),
    (2004, "BondPricer",            202),
    (2005, "YieldCurveAnalytics",   202),
    (2006, "DurationCalc",          202),
    (2007, "CDSPricer",             203),
    (2008, "StructuredDealMgmt",    203),
    (3001, "CreditRiskDashboard",   301),
    (3002, "PD_LGD_Model",          301),
    (3003, "MarketRiskVaR",         302),
    (3004, "StressTestPlatform",    302),
    (3005, "AMLScreening",          303),
    (3006, "SARReporting",          303),
    (4001, "CloudConsole",          401),
    (4002, "CostOptimizer",         401),
    (4003, "DataPipeline",          402),
    (4004, "DataCatalog",           402),
    (4005, "SOCMonitor",            403),
    (4006, "IdentityMgmt",          403),
    (5001, "FinancialReportingHub", 501),
    (5002, "TreasuryWorkstation",   502),
    (5003, "TaxCompliance",         503),
]

USERS = [
    ("U001", "alice.chen"),
    ("U002", "bob.smith"),
    ("U003", "carol.jones"),
    ("U004", "david.lee"),
    ("U005", "emma.wilson"),
    ("U006", "frank.brown"),
    ("U007", "grace.kim"),
    ("U008", "henry.davis"),
    ("U009", "iris.taylor"),
    ("U010", "james.martinez"),
    ("U011", "karen.anderson"),
    ("U012", "liam.thomas"),
    ("U013", "mary.jackson"),
    ("U014", "noah.white"),
    ("U015", "olivia.harris"),
    ("U016", "peter.martin"),
    ("U017", "quinn.garcia"),
    ("U018", "rachel.miller"),
    ("U019", "sam.rodriguez"),
    ("U020", "tanya.lewis"),
]

# ── user_app_access: each user gets 3-8 apps ─────────────────────────────────
app_ids = [a[0] for a in APPLICATIONS]
access = set()
for uid, _ in USERS:
    for aid in random.sample(app_ids, random.randint(3, 8)):
        access.add((uid, aid))
USER_APP_ACCESS = sorted(access)

# ── date helpers ──────────────────────────────────────────────────────────────
START = datetime(2026, 1, 1)
END   = datetime(2026, 3, 31, 23, 59, 59)

def rand_dt():
    delta = END - START
    return START + timedelta(seconds=random.randint(0, int(delta.total_seconds())))

WAREHOUSES = ["COMPUTE_WH", "ANALYTICS_WH", "REPORTING_WH", "ETL_WH"]

S3_BUCKETS = [
    ("raw-data-lake",       "lending/originations"),
    ("raw-data-lake",       "risk/credit-scores"),
    ("analytics-warehouse", "trading/equity"),
    ("analytics-warehouse", "trading/fixed-income"),
    ("ml-feature-store",    "models/pd-lgd"),
    ("reporting-archive",   "finance/gl-exports"),
    ("audit-trail-store",   "compliance/sar"),
    ("infra-logs",          "cloud/billing"),
]

# ── write CSV helpers ─────────────────────────────────────────────────────────
def write(name, headers, rows):
    p = os.path.join(OUT, f"{name}.csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    print(f"  wrote {len(rows):>4} rows → {p}")

# ── reference tables ──────────────────────────────────────────────────────────
write("lob",             ["lob_id","lob_name"],                              LOBS)
write("cost_center",     ["cost_center_id","cost_center_name","lob_id"],     COST_CENTERS)
write("application",     ["app_id","app_name","cost_center_id"],             APPLICATIONS)
write("users",           ["user_id","user_name"],                            USERS)
write("user_app_access", ["user_id","app_id"],                               USER_APP_ACCESS)

# ── dremio_usage ──────────────────────────────────────────────────────────────
dremio_rows = []
# Data Engineering and Analytics users query Dremio most
heavy_users = ["U013", "U014", "U015", "U003", "U007", "U012"]
for _ in range(400):
    uid = random.choice(heavy_users if random.random() < 0.7 else [u[0] for u in USERS])
    qid = f"DQ-{random.randint(100000,999999)}"
    qt  = round(random.uniform(0.5, 240), 2)      # query_time seconds
    qc  = round(qt * random.uniform(0.003, 0.012), 4)  # ~$0.003-0.012/sec
    dremio_rows.append((rand_dt().strftime("%Y-%m-%d %H:%M:%S"), uid, qid, qt, round(qc, 4)))
write("dremio_usage", ["datetime","user_id","query_id","query_time","query_cost"], dremio_rows)

# ── snowflake_usage ───────────────────────────────────────────────────────────
sf_rows = []
heavy_sf = ["U001", "U005", "U010", "U016", "U018", "U020"]
for _ in range(300):
    uid = random.choice(heavy_sf if random.random() < 0.65 else [u[0] for u in USERS])
    qid = f"SQ-{random.randint(100000,999999)}"
    qt  = round(random.uniform(1, 600), 2)
    wh  = random.choice(WAREHOUSES)
    # Snowflake cost = credits * $3/credit; XSMALL warehouse = 1 credit/hr
    credits = qt / 3600
    qc = round(credits * 3 * random.uniform(0.8, 2.5), 4)
    sf_rows.append((rand_dt().strftime("%Y-%m-%d %H:%M:%S"), uid, qid, qt, round(qc, 4), wh))
write("snowflake_usage", ["datetime","user_id","query_id","query_time","query_cost","warehouse_name"], sf_rows)

# ── s3_usage ──────────────────────────────────────────────────────────────────
s3_rows = []
for app_id, _, _ in APPLICATIONS:
    bucket, folder = random.choice(S3_BUCKETS)
    # Monthly snapshot, one row per app per month
    for month in range(1, 4):
        dt = datetime(2026, month, 28).strftime("%Y-%m-%d %H:%M:%S")
        gb = round(random.uniform(5, 2000), 2)
        cost = round(gb * 0.023, 4)   # S3 standard $0.023/GB
        s3_rows.append((dt, app_id, bucket, folder, cost))
write("s3_usage", ["datetime","application_id","s3_bucket","s3_folder","storage_cost"], s3_rows)

print("Done.")

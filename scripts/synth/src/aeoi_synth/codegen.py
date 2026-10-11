# ruff: noqa: S608  - generates source text that CONTAINS SQL; nothing here runs a query
"""Phase 14 (ADR-024): deterministic synthetic SOURCE CODE for every deployable service.

One repository per domain (`northwind/<domain>`, as in the catalog), one module per deployable
service (api / worker / stream) in the service's catalog language (java, python, go). Every
commit in `devdata.commits` becomes a real edit of a real file: the file's content is evolved
in commit order and each commit stores its unified diff + the file after the change
(`devdata.commit_diffs`). The repository at HEAD is what `make codepack` writes to disk and
`make code-index` parses.

Planted, whatever the seed:
- the demo commit (#812, checkout-api): `OrderRepository.findOpenOrders(...)` moved INSIDE the
  cart-item loop (a query per item) - the change behind the demo incident;
- a contract change in shipping-api: `ShipmentResponse.address` renamed to `deliveryAddress`,
  read by the shipping clients of ~9 other services (FR-7 "change impact").

Everything here uses its OWN random stream (seeded from the repo name), so adding code does not
shift any other synthetic data.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DEMO_SERVICE = "checkout-api"
IMPACT_SERVICE = "shipping-api"
IMPACT_MESSAGE = "Rename address to deliveryAddress in ShipmentResponse (#377)"

ENTITY = {
    "orders": "Order", "payments": "Payment", "inventory": "StockItem", "cart": "Cart",
    "checkout": "Cart", "catalog": "Product", "search": "SearchResult", "pricing": "Price",
    "shipping": "Shipment", "returns": "ReturnRequest", "notifications": "Notification",
    "identity": "User", "accounts": "Customer", "billing": "Bill", "ledger": "LedgerEntry",
    "fraud": "RiskCheck", "reviews": "Review", "media": "Asset",
    "recommendations": "Recommendation", "analytics": "Metric", "loyalty": "PointsBalance",
    "promotions": "Promotion", "tax": "TaxRate", "invoicing": "Invoice", "support": "Ticket",
    "chat": "Message", "scheduler": "Job", "reporting": "ReportRun", "partner": "Partner",
    "geo": "Location",
}  # fmt: skip


def snake(name: str) -> str:
    out = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
    return out


def domain_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


@dataclass
class Module:
    service_key: str
    kind: str  # api | worker | stream
    language: str
    domain: str
    entity: str
    files: dict[str, str] = field(default_factory=dict)  # path -> content
    roles: dict[str, str] = field(default_factory=dict)  # role -> path (config, service, ...)


@dataclass
class CodePack:
    repos: dict[str, dict[str, str]]  # repository -> {path: content at HEAD}
    diffs: list[dict[str, Any]]  # devdata.commit_diffs rows
    extra_commits: list[dict[str, Any]]  # planted commits added by codegen
    extra_prs: list[dict[str, Any]]

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for repo in sorted(self.repos):
            for path in sorted(self.repos[repo]):
                h.update(f"{repo}/{path}\n{self.repos[repo][path]}".encode())
        return h.hexdigest()[:16]


# ------------------------------------------------------------------ templates: java
def _java_response(m: Module, pkg: str) -> str:
    e = m.entity
    extra = "    private String address;\n" if m.service_key == IMPACT_SERVICE else ""
    getter = (
        "\n    public String getAddress() {\n        return address;\n    }\n"
        if m.service_key == IMPACT_SERVICE
        else ""
    )
    return f"""package {pkg};

/** Response body of {m.service_key}. Other services read it through their clients. */
public class {e}Response {{
    private String id;
    private String name;
    private String status;
{extra}
    public String getId() {{
        return id;
    }}

    public String getName() {{
        return name;
    }}

    public String getStatus() {{
        return status;
    }}
{getter}}}
"""


def _java_repository(m: Module, pkg: str, repo_name: str, entity: str) -> str:
    finder = (
        f"""
    public List<{entity}> findOpenOrders(String sku) {{
        return db.query("SELECT * FROM orders WHERE sku = ? AND status = 'OPEN'", sku);
    }}

    public List<{entity}> findOpenOrdersForCustomer(String customerId) {{
        return db.query("SELECT * FROM orders WHERE customer_id = ? AND status = 'OPEN'", customerId);
    }}
"""
        if m.service_key == DEMO_SERVICE
        else ""
    )
    return f"""package {pkg};

import java.util.List;

/** Database access for {m.service_key}. Every method is one round trip to the database. */
public class {repo_name} {{
    private final Database db;

    public {repo_name}(Database db) {{
        this.db = db;
    }}

    public {entity} findById(String id) {{
        return db.queryOne("SELECT * FROM {m.domain} WHERE id = ?", id);
    }}

    public List<{entity}> findAll(int limit) {{
        return db.query("SELECT * FROM {m.domain} ORDER BY id LIMIT ?", limit);
    }}

    public void save({entity} item) {{
        db.execute("INSERT INTO {m.domain} VALUES (?)", item);
    }}
{finder}}}
"""


def _java_config(m: Module, pkg: str) -> str:
    return f"""package {pkg};

/** Runtime limits for {m.service_key}. */
public final class Config {{
    public static final int TIMEOUT_MS = 2000;
    public static final int MAX_RETRIES = 3;
    public static final int POOL_SIZE = 20;
    public static final String CLIENT_VERSION = "1.0.0";

    private Config() {{
    }}
}}
"""


def _java_client(m: Module, pkg: str, dep: str, dep_entity: str) -> str:
    cls = "".join(p.title() for p in domain_of(dep).split("_")) + "Client"
    dep_pkg = f"com.northwind.{domain_of(dep)}"
    return f"""package {pkg}.clients;

import {dep_pkg}.{dep_entity}Response;

/** HTTP client for {dep}. */
public class {cls} {{
    private final HttpClient http;

    public {cls}(HttpClient http) {{
        this.http = http.withTimeout(1500);
    }}

    public {dep_entity}Response get{dep_entity}(String id) {{
        return http.get("/{domain_of(dep)}/" + id, {dep_entity}Response.class);
    }}
}}
"""


def _java_service(m: Module, pkg: str, deps: list[tuple[str, str]]) -> str:
    e = m.entity
    imports = "".join(
        f"import {pkg}.clients.{domain_of(d).title()}Client;\n"
        f"import com.northwind.{domain_of(d)}.{de}Response;\n"
        for d, de in deps
    )
    fields = "".join(
        f"    private final {domain_of(d).title()}Client {domain_of(d)}Client;\n" for d, _ in deps
    )
    params = "".join(f", {domain_of(d).title()}Client {domain_of(d)}Client" for d, _ in deps)
    assigns = "".join(
        f"        this.{domain_of(d)}Client = {domain_of(d)}Client;\n" for d, _ in deps
    )
    uses = "".join(
        f"""
    public String describe{de}(String id) {{
        {de}Response other = {domain_of(d)}Client.get{de}(id);
        return other.{"getAddress" if d == IMPACT_SERVICE else "getName"}();
    }}
"""
        for d, de in deps
    )
    return f"""package {pkg};

import java.util.List;
{imports}
/** Business logic of {m.service_key}. */
public class {e}Service {{
    private final {e}Repository repository;
{fields}
    public {e}Service({e}Repository repository{params}) {{
        this.repository = repository;
{assigns}    }}

    public {e} get{e}(String id) {{
        {e} found = repository.findById(id);
        if (found == null) {{
            throw new IllegalArgumentException("unknown {e.lower()} " + id);
        }}
        return found;
    }}

    public List<{e}> list(int limit) {{
        return repository.findAll(limit);
    }}
{uses}}}
"""


def _java_cart_service(pkg: str) -> str:
    """The demo service's cart pricing BEFORE commit #812: one query per cart."""
    return f"""package {pkg};

import java.util.List;

/** Prices a cart and reports the customer's open orders. */
public class CartService {{
    private final OrderRepository orderRepository;

    public CartService(OrderRepository orderRepository) {{
        this.orderRepository = orderRepository;
    }}

    public CartTotal priceCart(Cart cart) {{
        List<Order> open = orderRepository.findOpenOrdersForCustomer(cart.getCustomerId());
        long total = 0;
        for (CartItem item : cart.getItems()) {{
            total += item.getPrice() * item.getQuantity();
        }}
        return new CartTotal(total, open.size());
    }}
}}
"""


CART_AFTER_812 = """    public CartTotal priceCart(Cart cart) {
        long total = 0;
        int openOrders = 0;
        for (CartItem item : cart.getItems()) {
            total += item.getPrice() * item.getQuantity();
            List<Order> open = orderRepository.findOpenOrders(item.getSku());
            openOrders += open.size();
        }
        return new CartTotal(total, openOrders);
    }
"""


def _java_worker(m: Module, pkg: str) -> str:
    e = m.entity
    return f"""package {pkg};

import java.util.List;

/** Background worker of {m.service_key}: saves a batch of {e.lower()} records. */
public class {e}Worker {{
    private final {e}Repository repository;

    public {e}Worker({e}Repository repository) {{
        this.repository = repository;
    }}

    public int process(List<{e}> batch) {{
        int done = 0;
        for ({e} item : batch) {{
            repository.save(item);
            done++;
        }}
        return done;
    }}
}}
"""


def _java_consumer(m: Module, pkg: str) -> str:
    e = m.entity
    return f"""package {pkg};

/** Kafka consumer of {m.service_key}: one event at a time, idempotent on event id. */
public class {e}EventConsumer {{
    private final Dedupe dedupe;

    public {e}EventConsumer(Dedupe dedupe) {{
        this.dedupe = dedupe;
    }}

    public void onEvent(Event event) {{
        if (dedupe.seen(event.getId())) {{
            return;
        }}
        dedupe.remember(event.getId());
    }}
}}
"""


# ------------------------------------------------------------------ templates: python
def _py_config(m: Module) -> str:
    return f'''"""Runtime limits for {m.service_key}."""

TIMEOUT_MS = 2000
MAX_RETRIES = 3
POOL_SIZE = 20
CLIENT_VERSION = "1.0.0"
'''


def _py_models(m: Module) -> str:
    e = m.entity
    extra = '    address: str = ""\n' if m.service_key == IMPACT_SERVICE else ""
    return f'''"""Data shapes of {m.service_key}."""

from dataclasses import dataclass


@dataclass
class {e}Response:
    id: str
    name: str
    status: str
{extra}'''


def _py_repository(m: Module) -> str:
    e = m.entity
    return f'''"""Database access for {m.service_key}. Every function is one round trip."""


class {e}Repository:
    def __init__(self, db):
        self.db = db

    def find_by_id(self, item_id):
        return self.db.query_one("SELECT * FROM {m.domain} WHERE id = %s", item_id)

    def find_all(self, limit):
        return self.db.query("SELECT * FROM {m.domain} ORDER BY id LIMIT %s", limit)

    def save(self, item):
        self.db.execute("INSERT INTO {m.domain} VALUES (%s)", item)
'''


def _py_clients(m: Module, deps: list[tuple[str, str]]) -> str:
    body = "".join(
        f'''

class {domain_of(d).title()}Client:
    """HTTP client for {d}."""

    def __init__(self, http):
        self.http = http.with_timeout(1500)

    def get_{snake(de)}(self, item_id):
        return self.http.get(f"/{domain_of(d)}/{{item_id}}")
'''
        for d, de in deps
    )
    return f'"""HTTP clients of {m.service_key}."""\n{body or chr(10)}'


def _py_service(m: Module, deps: list[tuple[str, str]]) -> str:
    e = m.entity
    uses = "".join(
        f"""
    def describe_{snake(de)}(self, item_id):
        other = self.{domain_of(d)}_client.get_{snake(de)}(item_id)
        return other.{"address" if d == IMPACT_SERVICE else "name"}
"""
        for d, de in deps
    )
    params = "".join(f", {domain_of(d)}_client" for d, _ in deps)
    assigns = "".join(
        f"        self.{domain_of(d)}_client = {domain_of(d)}_client\n" for d, _ in deps
    )
    return f'''"""Business logic of {m.service_key}."""


class {e}Service:
    def __init__(self, repository{params}):
        self.repository = repository
{assigns}
    def get_{snake(e)}(self, item_id):
        found = self.repository.find_by_id(item_id)
        if found is None:
            raise KeyError(f"unknown {snake(e)} {{item_id}}")
        return found

    def list(self, limit):
        return self.repository.find_all(limit)
{uses}'''


def _py_worker(m: Module) -> str:
    e = m.entity
    return f'''"""Background worker of {m.service_key}: saves a batch of records."""


class {e}Worker:
    def __init__(self, repository):
        self.repository = repository

    def process(self, batch):
        done = 0
        for item in batch:
            self.repository.save(item)
            done += 1
        return done
'''


# ------------------------------------------------------------------ templates: go
def _go_config(m: Module) -> str:
    return f"""// Package {m.domain} holds {m.service_key}.
package {m.domain}

// Runtime limits for {m.service_key}.
const (
	TimeoutMs     = 2000
	MaxRetries    = 3
	PoolSize      = 20
	ClientVersion = "1.0.0"
)
"""


def _go_models(m: Module) -> str:
    extra = "\tAddress string\n" if m.service_key == IMPACT_SERVICE else ""
    return f"""package {m.domain}

// {m.entity}Response is the response body of {m.service_key}.
type {m.entity}Response struct {{
	ID     string
	Name   string
	Status string
{extra}}}
"""


def _go_repository(m: Module) -> str:
    e = m.entity
    return f"""package {m.domain}

// {e}Repository is the database access of {m.service_key}: one round trip per call.
type {e}Repository struct {{
	db DB
}}

func (r *{e}Repository) FindByID(id string) (*{e}Response, error) {{
	return r.db.QueryOne("SELECT * FROM {m.domain} WHERE id = $1", id)
}}

func (r *{e}Repository) FindAll(limit int) ([]{e}Response, error) {{
	return r.db.Query("SELECT * FROM {m.domain} ORDER BY id LIMIT $1", limit)
}}

func (r *{e}Repository) Save(item {e}Response) error {{
	return r.db.Exec("INSERT INTO {m.domain} VALUES ($1)", item)
}}
"""


def _go_clients(m: Module, deps: list[tuple[str, str]]) -> str:
    body = "".join(
        f"""
// {domain_of(d).title()}Client calls {d}.
type {domain_of(d).title()}Client struct {{
	http HTTP
}}

func (c *{domain_of(d).title()}Client) Get{de}(id string) (*{de}Response, error) {{
	return c.http.WithTimeout(1500).Get("/{domain_of(d)}/" + id)
}}
"""
        for d, de in deps
    )
    return f"package {m.domain}\n{body}"


def _go_service(m: Module, deps: list[tuple[str, str]]) -> str:
    e = m.entity
    fields = "".join(f"\t{domain_of(d)}Client *{domain_of(d).title()}Client\n" for d, _ in deps)
    uses = "".join(
        f"""
func (s *{e}Service) Describe{de}(id string) (string, error) {{
	other, err := s.{domain_of(d)}Client.Get{de}(id)
	if err != nil {{
		return "", err
	}}
	return other.{"Address" if d == IMPACT_SERVICE else "Name"}, nil
}}
"""
        for d, de in deps
    )
    return f"""package {m.domain}

// {e}Service is the business logic of {m.service_key}.
type {e}Service struct {{
	repository *{e}Repository
{fields}}}

func (s *{e}Service) Get{e}(id string) (*{e}Response, error) {{
	return s.repository.FindByID(id)
}}

func (s *{e}Service) List(limit int) ([]{e}Response, error) {{
	return s.repository.FindAll(limit)
}}
{uses}"""


def _go_worker(m: Module) -> str:
    e = m.entity
    return f"""package {m.domain}

// {e}Worker saves a batch of records for {m.service_key}.
type {e}Worker struct {{
	repository *{e}Repository
}}

func (w *{e}Worker) Process(batch []{e}Response) (int, error) {{
	done := 0
	for _, item := range batch {{
		if err := w.repository.Save(item); err != nil {{
			return done, err
		}}
		done++
	}}
	return done, nil
}}
"""


# ------------------------------------------------------------------ module layout
def build_module(svc: dict[str, Any], catalog: dict[str, dict[str, Any]]) -> Module:
    key, kind, lang = svc["key"], svc["type"], svc["language"]
    domain = domain_of(key)
    m = Module(key, kind, lang, domain, ENTITY.get(domain, domain.title()))
    deps = [
        (d, ENTITY.get(domain_of(d), domain_of(d).title()))
        for d in svc["depends_on"]
        if d.endswith("-api") and d in catalog
    ]
    if lang == "java":
        pkg = f"com.northwind.{domain}"
        base = f"services/{key}/src/main/java/com/northwind/{domain}"
        put = m.files.__setitem__
        put(f"{base}/Config.java", _java_config(m, pkg))
        m.roles["config"] = f"{base}/Config.java"
        if key == DEMO_SERVICE:
            put(
                f"{base}/OrderRepository.java", _java_repository(m, pkg, "OrderRepository", "Order")
            )
            put(f"{base}/CartService.java", _java_cart_service(pkg))
            m.roles.update(repository=f"{base}/OrderRepository.java",
                           service=f"{base}/CartService.java")  # fmt: skip
        elif kind == "stream":
            put(f"{base}/{m.entity}EventConsumer.java", _java_consumer(m, pkg))
            m.roles["service"] = f"{base}/{m.entity}EventConsumer.java"
        else:
            repo = f"{base}/{m.entity}Repository.java"
            put(repo, _java_repository(m, pkg, f"{m.entity}Repository", m.entity))
            m.roles["repository"] = repo
            if kind == "api":
                put(f"{base}/{m.entity}Response.java", _java_response(m, pkg))
                put(f"{base}/{m.entity}Service.java", _java_service(m, pkg, deps))
                m.roles["model"] = f"{base}/{m.entity}Response.java"
                m.roles["service"] = f"{base}/{m.entity}Service.java"
            else:
                put(f"{base}/{m.entity}Worker.java", _java_worker(m, pkg))
                m.roles["service"] = f"{base}/{m.entity}Worker.java"
        for d, de in deps if kind == "api" else []:
            path = f"{base}/clients/{domain_of(d).title()}Client.java"
            put(path, _java_client(m, pkg, d, de))
            m.roles.setdefault("client", path)
    elif lang == "python":
        base = f"services/{key}/{snake(key.replace('-', '_'))}"
        m.files[f"{base}/config.py"] = _py_config(m)
        m.files[f"{base}/repository.py"] = _py_repository(m)
        m.roles.update(config=f"{base}/config.py", repository=f"{base}/repository.py")
        if kind == "api":
            m.files[f"{base}/models.py"] = _py_models(m)
            m.files[f"{base}/service.py"] = _py_service(m, deps)
            m.files[f"{base}/clients.py"] = _py_clients(m, deps)
            m.roles.update(model=f"{base}/models.py", service=f"{base}/service.py",
                           client=f"{base}/clients.py")  # fmt: skip
        else:
            m.files[f"{base}/worker.py"] = _py_worker(m)
            m.roles["service"] = f"{base}/worker.py"
    else:  # go
        base = f"services/{key}/internal/{domain}"
        m.files[f"{base}/config.go"] = _go_config(m)
        m.files[f"{base}/repository.go"] = _go_repository(m)
        m.files[f"{base}/models.go"] = _go_models(m)
        m.roles.update(config=f"{base}/config.go", repository=f"{base}/repository.go",
                       model=f"{base}/models.go")  # fmt: skip
        if kind == "api":
            m.files[f"{base}/service.go"] = _go_service(m, deps)
            m.files[f"{base}/clients.go"] = _go_clients(m, deps)
            m.roles.update(service=f"{base}/service.go", client=f"{base}/clients.go")
        else:
            m.files[f"{base}/worker.go"] = _go_worker(m)
            m.roles["service"] = f"{base}/worker.go"
    return m


# ------------------------------------------------------------------ edits (one per commit)
def _replace_const(text: str, lang: str, name: str, new: str) -> str:
    names = {
        "java": name,
        "python": name,
        "go": "".join(p.title() for p in name.lower().split("_")),
    }
    target = names[lang]
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if lang == "java" and f" {target} = " in line:
            line = line[: line.index("=") + 2] + new + ";\n"
        elif lang == "python" and stripped.startswith(f"{target} = "):
            line = f"{target} = {new}\n"
        elif lang == "go" and stripped.startswith(f"{target} ") and "=" in stripped:
            indent = line[: len(line) - len(line.lstrip())]
            pad = line[len(indent) :].split("=")[0]
            line = f"{indent}{pad}= {new}\n"
        out.append(line)
    return "".join(out)


def _append_member(text: str, lang: str, member: str) -> str:
    """Insert a method before the class's closing brace (java), at the end (python/go)."""
    if lang == "java":
        idx = text.rstrip().rfind("}")
        return text[:idx] + member + text[idx:]
    return text.rstrip("\n") + "\n" + member


def _edit(
    m: Module, files: dict[str, str], message: str, n: int, rnd: random.Random
) -> tuple[str, str]:
    """(path, new content) for one ordinary commit. Deterministic per (module, n)."""
    lang = m.language
    cfg = m.roles["config"]
    if message.startswith("Tune timeouts"):
        return cfg, _replace_const(
            files[cfg], lang, "TIMEOUT_MS", str(rnd.choice((800, 1500, 3000, 5000)))
        )
    if message.startswith("Improve retries"):
        return cfg, _replace_const(files[cfg], lang, "MAX_RETRIES", str(rnd.choice((1, 2, 4, 5))))
    if message.startswith("Bump deps"):
        return cfg, _replace_const(files[cfg], lang, "CLIENT_VERSION", f'"1.{n}.0"')
    repo = m.roles.get("repository", cfg)
    svc = m.roles.get("service", repo)
    if message.startswith("Add pagination") and repo != cfg:
        member = {
            "java": f"\n    public List<{m.entity if m.service_key != DEMO_SERVICE else 'Order'}> findPage{n}(int offset, int limit) {{\n"
            f'        return db.query("SELECT * FROM {m.domain} ORDER BY id OFFSET ? LIMIT ?", offset, limit);\n    }}\n',
            "python": f"\n    def find_page_{n}(self, offset, limit):\n"
            f'        return self.db.query("SELECT * FROM {m.domain} ORDER BY id OFFSET %s LIMIT %s", offset, limit)\n',
            "go": f"\nfunc (r *{m.entity}Repository) FindPage{n}(offset, limit int) ([]{m.entity}Response, error) {{\n"
            f'\treturn r.db.Query("SELECT * FROM {m.domain} ORDER BY id OFFSET $1 LIMIT $2", offset, limit)\n}}\n',
        }[lang]
        return repo, _append_member(files[repo], lang, member)
    if message.startswith("Add metrics"):
        member = {
            "java": f'\n    public void recordMetric{n}(Metrics metrics) {{\n        metrics.increment("{m.service_key}.calls");\n    }}\n',
            "python": f'\n    def record_metric_{n}(self, metrics):\n        metrics.increment("{m.service_key}.calls")\n',
            "go": f'\nfunc RecordMetric{n}(metrics Metrics) {{\n\tmetrics.Increment("{m.service_key}.calls")\n}}\n',
        }[lang]
        return svc, _append_member(files[svc], lang, member)
    if message.startswith("Fix null handling"):
        member = {
            "java": f"\n    public boolean isValid{n}(String id) {{\n        return id != null && !id.isBlank();\n    }}\n",
            "python": f'\n    def is_valid_{n}(self, item_id):\n        return item_id is not None and item_id != ""\n',
            "go": f'\nfunc IsValid{n}(id string) bool {{\n\treturn id != ""\n}}\n',
        }[lang]
        return svc, _append_member(files[svc], lang, member)
    # Refactor client: the client timeout (or, without clients, the service gets a helper)
    client = m.roles.get("client")
    if client:
        text = files[client]
        new = str(rnd.choice((1000, 1200, 2000, 2500)))
        for old in ("withTimeout(", "with_timeout(", "WithTimeout("):
            i = text.find(old)
            if i >= 0:
                j = text.index(")", i)
                text = text[: i + len(old)] + new + text[j:]
        return client, text
    return svc, _append_member(files[svc], lang, _label(m, n))


def _label(m: Module, n: int) -> str:
    return {
        "java": f'\n    private String label{n}(String id) {{\n        return "{m.domain}:" + id;\n    }}\n',
        "python": f'\n    def _label_{n}(self, item_id):\n        return f"{m.domain}:{{item_id}}"\n',
        "go": f'\nfunc label{n}(id string) string {{\n\treturn "{m.domain}:" + id\n}}\n',
    }[m.language]


def _demo_edit(files: dict[str, str], m: Module) -> tuple[str, str]:
    path = m.roles["service"]
    text = files[path]
    start = text.index("    public CartTotal priceCart(Cart cart) {")
    end = text.index("    }\n", text.index("return new CartTotal", start)) + len("    }\n")
    return path, text[:start] + CART_AFTER_812 + text[end:]


def _impact_edit(files: dict[str, str], m: Module) -> tuple[str, str]:
    path = m.roles["model"]
    text = files[path]
    if m.language == "java":
        text = text.replace("private String address;", "private String deliveryAddress;")
        text = text.replace("public String getAddress() {\n        return address;",
                            "public String getDeliveryAddress() {\n        return deliveryAddress;")  # fmt: skip
    elif m.language == "python":
        text = text.replace("    address: str", "    delivery_address: str")
    else:
        text = text.replace("\tAddress string", "\tDeliveryAddress string")
    return path, text


def _diff(path: str, before: str, after: str) -> tuple[str, int, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        )
    )  # fmt: skip
    adds = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    dels = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    return "".join(lines), adds, dels


REPO_ACL_FILE = ".aeoi-repo.json"
DEFAULT_GROUPS = ["eng-all"]


def write_repos(pack: CodePack, root: Path) -> dict[str, Any]:
    """Write every repository at HEAD under `root/<org>/<repo>/`, each with an EXPLICIT ACL
    file (the indexer refuses a repository without one). Files that are no longer generated
    are not deleted: run on a clean tree (`data/sample-repositories` is gitignored)."""
    n = 0
    for repo, files in sorted(pack.repos.items()):
        base = root / repo
        for path, content in sorted(files.items()):
            target = base / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            n += 1
        services = sorted({p.split("/")[1] for p in files if p.startswith("services/")})
        (base / REPO_ACL_FILE).write_text(
            json.dumps(
                {"repository": repo, "allowed_groups": DEFAULT_GROUPS, "services": services},
                indent=1,
            )
            + "\n"
        )
    return {"repositories": len(pack.repos), "files": n, "fingerprint": pack.fingerprint()}


def build(
    catalog: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    demo_sha: str,
    impact_at: datetime,
    sha_fn: Any,
    seed: int,
) -> CodePack:
    """Evolve every module through its commits (in time order). Mutates `commits` and `prs`
    in place: files_changed / changed_files / additions / deletions become the real ones."""
    by_key = {s["key"]: s for s in catalog}
    modules = {
        s["key"]: build_module(s, by_key)
        for s in catalog
        if s["type"] in ("api", "worker", "stream")
    }
    files: dict[str, dict[str, str]] = {}  # service -> path -> content
    for key, mod in modules.items():
        files[key] = dict(mod.files)
    extra_commits: list[dict[str, Any]] = []
    extra_prs: list[dict[str, Any]] = []
    imp = modules.get(IMPACT_SERVICE)
    if imp is not None and "model" in imp.roles:
        sha = sha_fn("impact", "shipment-address")
        extra_commits.append({
            "sha": sha, "repository": by_key[IMPACT_SERVICE]["repository"],
            "service_key": IMPACT_SERVICE, "author": "Ravi Iyer", "message": IMPACT_MESSAGE,
            "files_changed": [], "additions": 0, "deletions": 0, "committed_at": impact_at,
        })  # fmt: skip
        extra_prs.append({
            "repository": by_key[IMPACT_SERVICE]["repository"], "number": 377,
            "title": "Rename ShipmentResponse.address to deliveryAddress",
            "body": "Clearer name. Clients should switch to deliveryAddress.",
            "author": "Ravi Iyer", "state": "MERGED", "changed_files": [],
            "merge_commit_sha": sha, "created_at": impact_at - timedelta(hours=6),
            "merged_at": impact_at,
        })  # fmt: skip
    diffs: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for c in sorted([*commits, *extra_commits], key=lambda r: (r["committed_at"], r["sha"])):
        m = modules.get(c["service_key"])
        if m is None:
            continue
        n = counters[m.service_key] = counters.get(m.service_key, 0) + 1
        rnd = random.Random(f"{seed}:{c['sha']}")
        fs = files[m.service_key]
        if c["sha"] == demo_sha and m.service_key == DEMO_SERVICE:
            path, after = _demo_edit(fs, m)
        elif c["message"] == IMPACT_MESSAGE:
            path, after = _impact_edit(fs, m)
        else:
            path, after = _edit(m, fs, c["message"], n, rnd)
            if after == fs[path]:  # the random pick equalled the old value: a commit always changes
                path, after = _edit(m, fs, "Housekeeping", n, rnd)
                if after == fs[path]:
                    path = m.roles.get("service", m.roles["config"])
                    after = _append_member(fs[path], m.language, _label(m, n))
        patch, adds, dels = _diff(path, fs[path], after)
        fs[path] = after
        c["files_changed"] = [path]
        c["additions"], c["deletions"] = adds, dels
        diffs.append({"sha": c["sha"], "path": path, "patch": patch, "content_after": after,
                      "additions": adds, "deletions": dels})  # fmt: skip
    for pr in [*prs, *extra_prs]:
        src = next(
            (c for c in [*commits, *extra_commits] if c["sha"] == pr["merge_commit_sha"]), None
        )
        if src is not None:
            pr["changed_files"] = list(src["files_changed"])
    repos: dict[str, dict[str, str]] = {}
    for key, fs in files.items():
        repos.setdefault(by_key[key]["repository"], {}).update(fs)
    return CodePack(repos=repos, diffs=diffs, extra_commits=extra_commits, extra_prs=extra_prs)

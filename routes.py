"""The 21 Amazon endpoints.

Every path is served with and without the `/amazon` prefix, so code
generated against the hosted API (paths like /products/details) runs
unchanged against this server.

Authentication:
    All Amazon scraper endpoints require the X-API-Key HTTP header.

    Example:
        curl -H "X-API-Key: YOUR_SECRET_KEY" \
            "http://localhost:8000/products/details?product=B07QSFHT27"

    /health remains public so Render health checks can access it without
    authentication.

Environment variable:
    SCRAPER_API_KEY

Params are validated by the package's own marshmallow schemas
(amazon/schemas.py): ONE param per input — `product` takes an ASIN or a
product link, `seller` an id or a seller link, and so on — and a pasted
amazon.de / amazon.co.uk … link picks that marketplace by itself.
"""

import json
import os
import secrets
from urllib.parse import urlencode

from bottle import request, response, route

from amazon import influencers, products, rankings, schemas, search, sellers
from schema_fields import load_query
from scraper_errors import BadRequest, Blocked, NotFound, UpstreamError


# ---------------------------------------------------------------------------
# API AUTHENTICATION
# ---------------------------------------------------------------------------

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")


def require_api_key():
    """Require a valid X-API-Key header.

    Response behavior:
        500 -> server is not configured with SCRAPER_API_KEY
        401 -> API key was not supplied
        403 -> API key was supplied but is incorrect
    """

    # The server itself is incorrectly configured.
    # Do not allow unauthenticated scraper requests in this situation.
    if not SCRAPER_API_KEY:
        response.status = 500
        response.content_type = "application/json"
        return json.dumps(
            {
                "error": "SCRAPER_API_KEY is not configured on the server"
            }
        )

    # Read the API key from the HTTP header:
    #
    #     X-API-Key: your-secret-key
    #
    provided_key = request.headers.get("X-API-Key")

    # No key supplied.
    if not provided_key:
        response.status = 401
        response.content_type = "application/json"
        return json.dumps(
            {
                "error": "API key required"
            }
        )

    # Compare secrets using constant-time comparison.
    if not secrets.compare_digest(provided_key, SCRAPER_API_KEY):
        response.status = 403
        response.content_type = "application/json"
        return json.dumps(
            {
                "error": "Invalid API key"
            }
        )

    # Authentication successful.
    return None


# ---------------------------------------------------------------------------
# RESPONSE HELPERS
# ---------------------------------------------------------------------------

def json_response(data, status=200):
    response.status = status
    response.content_type = "application/json"
    return json.dumps(data, ensure_ascii=False)


def query_dict():
    """The query as unicode strings (bottle 0.12's .get() hands back latin-1
    decoded bytes, so a UTF-8 "Café" would arrive as "CafÃ©")."""
    return {
        key: request.query.getunicode(key)
        for key in request.query.keys()
    }


def _page_link(path, params, page):
    if not page:
        return None

    query = {
        k: v
        for k, v in params.items()
        if v not in (None, "")
    }

    query["page"] = page

    return (
        f"{request.urlparts.scheme}://"
        f"{request.urlparts.netloc}"
        f"{path}?{urlencode(query)}"
    )


def paginate(result, path, params):
    """Lift the endpoint's `pagination` block into the flat shape the hosted
    API returns: count / per_page / current_page / total_pages / next / previous.
    """

    pagination = result.pop("pagination", None) or {}

    page = int(
        pagination.get("page")
        or params.get("page")
        or 1
    )

    total_pages = int(
        pagination.get("total_pages")
        or 0
    )

    out = {
        "count": pagination.get("total_count"),
        "per_page": pagination.get("items_per_page"),
        "current_page": page,
        "total_pages": total_pages,
        "next": _page_link(
            path,
            params,
            page + 1 if page < total_pages else None
        ),
        "previous": _page_link(
            path,
            params,
            page - 1 if page > 1 else None
        ),
    }

    out.update(result)

    return out


# ---------------------------------------------------------------------------
# REQUEST HANDLER
# ---------------------------------------------------------------------------

def handle(schema, impl, paginated):
    """Validate -> call -> map errors.

    Authentication is performed before the Amazon scraper is called.

    Errors:
        401 -> missing API key
        403 -> invalid API key
        400 -> bad parameters
        404 -> missing entity
        502 -> Amazon blocks / transport failures
        500 -> unexpected error
    """

    # ---------------------------------------------------------------
    # AUTHENTICATION
    # ---------------------------------------------------------------

    auth_error = require_api_key()

    if auth_error is not None:
        return auth_error

    # ---------------------------------------------------------------
    # QUERY VALIDATION
    # ---------------------------------------------------------------

    raw = query_dict()

    data, error = load_query(schema, raw)

    if error:
        return json_response(error, 400)

    # ---------------------------------------------------------------
    # AMAZON SCRAPER CALL
    # ---------------------------------------------------------------

    try:
        result = impl(**data)

    except ValueError as e:
        return json_response(
            {"error": str(e)},
            400
        )

    except BadRequest as e:
        return json_response(
            {"error": f"amazon rejected the request: {e}"},
            400
        )

    except NotFound as e:
        return json_response(
            {"error": str(e) or "not found"},
            404
        )

    except Blocked as e:
        return json_response(
            {
                "error":
                    f"amazon blocked the request, retry later: {e}"
            },
            502
        )

    except UpstreamError as e:
        return json_response(
            {
                "error":
                    f"amazon request failed: {e}"
            },
            502
        )

    except Exception as e:
        return json_response(
            {
                "error":
                    f"{type(e).__name__}: {e}"
            },
            500
        )

    # ---------------------------------------------------------------
    # PAGINATION
    # ---------------------------------------------------------------

    if paginated:
        result = paginate(
            result,
            request.path,
            raw
        )

    return json_response(result)


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------

ENDPOINTS = [
    # path, schema, function, paginated

    (
        "/products/details",
        schemas.ProductDetailsSchema,
        products.details,
        False
    ),

    (
        "/search/autocomplete",
        schemas.AutocompleteSchema,
        search.autocomplete,
        False
    ),

    (
        "/search",
        schemas.SearchSchema,
        search.search,
        True
    ),

    (
        "/products/reviews",
        schemas.ProductSchema,
        products.reviews,
        False
    ),

    (
        "/products/offers",
        schemas.OffersSchema,
        products.offers,
        True
    ),

    (
        "/products/variations",
        schemas.ProductSchema,
        products.variations,
        False
    ),

    (
        "/products/bulk",
        schemas.BulkSchema,
        products.bulk,
        False
    ),

    (
        "/products/lookup",
        schemas.LookupSchema,
        search.lookup,
        False
    ),

    (
        "/best-sellers",
        schemas.BestsellersSchema,
        rankings.bestsellers,
        True
    ),

    (
        "/best-sellers/categories",
        schemas.BestsellerCategoriesSchema,
        rankings.bestseller_categories,
        False
    ),

    (
        "/deals",
        schemas.DealsSchema,
        rankings.deals,
        True
    ),

    (
        "/categories",
        schemas.CategoriesSchema,
        search.categories,
        False
    ),

    (
        "/categories/tree",
        schemas.CategoryTreeSchema,
        search.category_tree,
        False
    ),

    (
        "/categories/products",
        schemas.CategoryProductsSchema,
        search.category_products,
        True
    ),

    (
        "/sellers/details",
        schemas.SellerSchema,
        sellers.profile,
        False
    ),

    (
        "/sellers/feedback",
        schemas.SellerFeedbackSchema,
        sellers.feedback,
        True
    ),

    (
        "/sellers/products",
        schemas.SellerProductsSchema,
        sellers.products,
        True
    ),

    (
        "/influencers/details",
        schemas.InfluencerSchema,
        influencers.profile,
        False
    ),

    (
        "/influencers/posts",
        schemas.InfluencerPostsSchema,
        influencers.posts,
        False
    ),

    (
        "/influencers/posts/products",
        schemas.InfluencerPostSchema,
        influencers.post_products,
        False
    ),

    (
        "/scrape",
        schemas.ScrapeUrlSchema,
        products.scrape_url,
        False
    ),
]


# ---------------------------------------------------------------------------
# ROUTE MOUNTING
# ---------------------------------------------------------------------------

def mount(path, schema, impl, paginated):
    """Serve an authenticated endpoint at /path and /amazon/path."""

    def handler():
        return handle(
            schema,
            impl,
            paginated
        )

    handler.__name__ = (
        "amazon_"
        + path.strip("/")
        .replace("/", "_")
        .replace("-", "_")
    )

    # Normal hosted API path.
    route(
        path,
        method="GET"
    )(handler)

    # /amazon-prefixed compatibility path.
    route(
        "/amazon" + path,
        method="GET"
    )(handler)


for _path, _schema, _impl, _paginated in ENDPOINTS:
    mount(
        _path,
        _schema,
        _impl,
        _paginated
    )


# ---------------------------------------------------------------------------
# PUBLIC HEALTH CHECK
# ---------------------------------------------------------------------------

@route("/", method="GET")
@route("/health", method="GET")
def health():
    """Public health endpoint.

    This endpoint intentionally does NOT require X-API-Key.

    Render can use:
        GET /health

    to determine whether the service is running.
    """

    return json_response(
        {
            "status": "ok",
            "endpoints": [
                endpoint[0]
                for endpoint in ENDPOINTS
            ]
        }
    )

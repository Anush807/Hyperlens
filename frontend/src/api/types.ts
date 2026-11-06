/**
 * Application-facing names for the API's response shapes.
 *
 * Every type here is an alias into `schema.d.ts`, which is generated from the
 * live FastAPI OpenAPI document by `npm run gen:types` (spec 11.2: do not
 * hand-write API types). Regenerate it after changing a response model and the
 * compiler will point at whatever broke.
 *
 * The aliases exist so components import `VM` rather than
 * `components["schemas"]["VMOut"]`, and so renaming a server model is a
 * one-line change here instead of a sweep through the pages.
 */
import type { components } from "@/api/schema";

type S = components["schemas"];

// --- enums ----------------------------------------------------------------

export type PowerState = NonNullable<S["VMOut"]["power_state"]>;
export type SourceType = NonNullable<S["SourceOut"]["type"]>;
export type SourceStatus = NonNullable<S["SourceOut"]["last_status"]>;
export type AnomalyStatus = NonNullable<S["AnomalyOut"]["status"]>;
export type RecommendationKind = NonNullable<S["RecommendationOut"]["kind"]>;
export type RecommendationStatus = NonNullable<S["RecommendationOut"]["status"]>;
export type EntityType = NonNullable<S["AnomalyOut"]["entity_type"]>;
export type ForecastModel = NonNullable<S["ForecastOut"]["model"]>;
export type Resolution = NonNullable<S["MetricSeries"]["resolution"]>;

// --- pagination -----------------------------------------------------------

/**
 * The server emits one concrete schema per paginated type (`Page_VMOut_`), so
 * this restates the envelope generically. The aliases below keep it honest by
 * asserting the generated shape is assignable to it.
 */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type VMPage = S["Page_VMOut_"];
export type HostPage = S["Page_HostOut_"];
export type ClusterPage = S["Page_ClusterOut_"];
export type DatastorePage = S["Page_DatastoreOut_"];
export type AnomalyPage = S["Page_AnomalyOut_"];

// --- inventory ------------------------------------------------------------

export type Source = S["SourceOut"];
export type SourceCreate = S["SourceCreate"];
export type SourceUpdate = S["SourceUpdate"];
export type SyncResult = S["SyncResult"];
export type Cluster = S["ClusterOut"];
export type Host = S["HostOut"];
export type VM = S["VMOut"];
export type Datastore = S["DatastoreOut"];
export type ClusterAllocation = S["ClusterAllocation"];
export type InventorySummary = S["InventorySummary"];

// --- metrics --------------------------------------------------------------

export type MetricRow = S["MetricRow"];
export type MetricSeries = S["MetricSeries"];

// --- ML -------------------------------------------------------------------

export type Anomaly = S["AnomalyOut"];
export type ForecastPoint = S["ForecastPoint"];
export type Forecast = S["ForecastOut"];
export type EntityForecasts = S["EntityForecasts"];
export type Capacity = S["CapacityOut"];
export type Recommendation = S["RecommendationOut"];
export type RecommendationPage = S["RecommendationPage"];
export type MlStatus = S["MlStatus"];

// --- assistant ------------------------------------------------------------

export type AssistantMessage = S["MessageOut"];
export type Conversation = S["ConversationOut"];

// --- auth -----------------------------------------------------------------

export type TokenPair = S["TokenPair"];
export type UserOut = S["UserOut"];
export type Role = NonNullable<S["UserOut"]["role"]>;

export type VMDetail = S["VMDetail"];
export type LatestMetrics = S["LatestMetrics"];

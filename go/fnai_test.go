package fnai

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func stub(t *testing.T) (*httptest.Server, *Client) {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/functions/7/deployment", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer fnp_test" {
			w.WriteHeader(401)
			json.NewEncoder(w).Encode(map[string]string{"detail": "authentication required"})
			return
		}
		n := 3
		json.NewEncoder(w).Encode(DeploymentOut{ID: 1, FunctionID: 7, VersionID: 9,
			VersionNumber: &n, Status: "active", Action: "deploy"})
	})
	mux.HandleFunc("POST /api/functions/7/invoke-keys", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(201)
		json.NewEncoder(w).Encode(map[string]any{"id": 5, "name": "ci", "hint": "abcd",
			"status": "active", "key": "fnk_plain"})
	})
	mux.HandleFunc("DELETE /api/auth/tokens/tid", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(204)
	})
	mux.HandleFunc("GET /api/experiments/99", func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"id": 99, "status": "completed"})
	})
	mux.HandleFunc("POST /v1/run/demo", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Input map[string]any `json:"input"`
		}
		json.NewDecoder(r.Body).Decode(&body)
		if body.Input["fail"] == true {
			// failure: error present, output null — matches the frozen /v1 contract
			json.NewEncoder(w).Encode(map[string]any{"output": nil, "status": "failed",
				"error": "boom", "meta": map[string]any{"version": 3, "latency_ms": 5,
					"cost_usd": 0.0, "tokens_in": 1, "tokens_out": 0, "trace_id": "t1"}})
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"output": map[string]any{"x": 1},
			"status": "success", "meta": map[string]any{"version": 3, "latency_ms": 12,
				"cost_usd": 0.000016, "tokens_in": 28, "tokens_out": 18, "trace_id": "t2"}})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, NewClient("fnp_test", WithBaseURL(srv.URL))
}

func TestTypedDeploymentRoundTrip(t *testing.T) {
	_, c := stub(t)
	dep, err := c.Deployments.Current(7)
	if err != nil {
		t.Fatal(err)
	}
	if dep.VersionID != 9 || *dep.VersionNumber != 3 || dep.Status != "active" {
		t.Fatalf("bad deployment decode: %+v", dep)
	}
}

func TestRawReachesAnUntypedEndpoint(t *testing.T) {
	_, c := stub(t)
	var out struct {
		ID     int    `json:"id"`
		Status string `json:"status"`
	}
	if err := c.Raw("GET", "/api/experiments/99", nil, &out); err != nil {
		t.Fatal(err)
	}
	if out.ID != 99 || out.Status != "completed" {
		t.Fatalf("bad Raw decode: %+v", out)
	}
}

func TestAPIErrorSurfacesStatusAndDetail(t *testing.T) {
	srv, _ := stub(t)
	bad := NewClient("fnp_wrong", WithBaseURL(srv.URL))
	_, err := bad.Deployments.Current(7)
	apiErr, ok := err.(*Error)
	if !ok || apiErr.Status != 401 || apiErr.Detail != "authentication required" {
		t.Fatalf("expected 401 Error, got %v", err)
	}
}

func TestMintKeyAndRunBothPaths(t *testing.T) {
	srv, c := stub(t)
	key, err := c.Keys.Invoke.Mint(7, "ci")
	if err != nil || key.Key != "fnk_plain" || key.ID != 5 {
		t.Fatalf("mint: %v %+v", err, key)
	}
	fn := c.Function("demo", key.Key)
	res, err := fn.Run(map[string]any{"document": "hi"})
	if err != nil || !res.OK() || res.Meta.TraceID != "t2" || res.Meta.TokensIn != 28 {
		t.Fatalf("success path: %v %+v", err, res)
	}
	res, err = fn.Run(map[string]any{"fail": true})
	if err != nil {
		t.Fatalf("execution failure must not be a Go error: %v", err)
	}
	if res.OK() || res.Error != "boom" || res.Output != nil {
		t.Fatalf("failure path decode: %+v", res)
	}
	_ = srv
}

func TestRevokeNoContent(t *testing.T) {
	_, c := stub(t)
	if err := c.Auth.Tokens.Revoke("tid"); err != nil {
		t.Fatal(err)
	}
}

// The functionalAI golden path in Go: create → version → playground →
// approve → deploy → mint invoke key → run → observe → archive.
//
// Against a dev server (FNAI_AUTH_MODE=test): set FNAI_BASE, run.
// Against staging: also set FNAI_PAT to a personal access token.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"time"

	fnai "github.com/ferrouslabs-git/fnai-public/go"
)

func main() {
	base := os.Getenv("FNAI_BASE")
	if base == "" {
		base = "http://127.0.0.1:8000"
	}
	pat := os.Getenv("FNAI_PAT")
	if pat == "" {
		// dev-auth bootstrap: mint a PAT as the default admin
		body, _ := json.Marshal(map[string]string{"name": "go-golden-path"})
		resp, err := http.Post(base+"/api/auth/tokens", "application/json",
			bytes.NewReader(body))
		if err != nil {
			log.Fatal(err)
		}
		defer resp.Body.Close()
		var minted struct {
			Token  string `json:"token"`
			Prefix string `json:"prefix"`
		}
		json.NewDecoder(resp.Body).Decode(&minted)
		pat = minted.Token
		fmt.Printf("PAT minted: %s…\n", minted.Prefix)
	}

	c := fnai.NewClient(pat, fnai.WithBaseURL(base))
	me, err := c.Me()
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("me():", me["roles"])

	slug := fmt.Sprintf("go_demo_%d", time.Now().Unix())
	fn, err := c.Functions.Create(fnai.FunctionCreate{
		Slug: slug, Name: "Go Demo",
		InputSchema: map[string]any{"type": "object",
			"properties": map[string]any{"document": map[string]any{"type": "string"}}},
		OutputSchema: map[string]any{"type": "object",
			"required": []string{"supplier", "total"},
			"properties": map[string]any{
				"supplier": map[string]any{"type": "string"},
				"total":    map[string]any{"type": "number"}}},
	})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("Functions.Create →", fn.ID, fn.Slug)

	v, err := c.Functions.CreateVersion(fn.ID, map[string]any{
		"sections": []map[string]any{{"type": "role", "title": "Role",
			"content": "You extract data from {document}."}},
		"impl":          "prompt",
		"model_binding": map[string]any{"model": "mock-small", "temperature": 0.2},
		"tools":         []any{},
	}, "via Go SDK")
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("Functions.CreateVersion →", v.ID)

	play, err := c.Versions.Playground(v.ID,
		map[string]any{"document": "Invoice, total 12.30"}, "")
	if err != nil {
		log.Fatal(err)
	}
	out, _ := json.Marshal(play.Output)
	fmt.Println("Versions.Playground →", string(out))

	if err := c.Versions.Submit(v.ID); err != nil {
		log.Fatal(err)
	}
	if err := c.Versions.Approve(v.ID, "go golden path"); err != nil {
		log.Fatal(err)
	}
	if _, err := c.Deployments.Deploy(fn.ID, v.ID); err != nil {
		log.Fatal(err)
	}
	dep, _ := c.Deployments.Current(fn.ID)
	fmt.Println("Deployments.Deploy →", dep.VersionID)

	key, err := c.Keys.Invoke.Mint(fn.ID, "go-demo")
	if err != nil {
		log.Fatal(err)
	}
	handle := c.Function(slug, key.Key)
	contract, err := handle.Contract()
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println("contract →", contract["function"], "v", contract["version"])
	result, err := handle.Run(map[string]any{"document": "Invoice from Acme Ltd, total 42.50"})
	if err != nil {
		log.Fatal(err)
	}
	fmt.Printf("run → ok:%v | trace:%s | cost:%v\n",
		result.OK(), result.Meta.TraceID, result.Meta.CostUSD)

	traces, _ := c.Traces.List(fn.ID, "production", 10)
	health, _ := c.Functions.Health(fn.ID, 24)
	fmt.Println("traces:", len(traces), "| health calls:", health.Calls)

	if err := c.Functions.Archive(fn.ID); err != nil {
		log.Fatal(err)
	}
	fmt.Println("cleanup: demo function archived")
	fmt.Println("\nGo golden path complete — every call through the fnai client")
}

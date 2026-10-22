package fnai

import (
	"bytes"
	"os"
	"reflect"
	"strings"
	"testing"
)

func removeSetup(t *testing.T, status string) (*fakeAPI, *Client) {
	api, c := newFakeAPI(t)
	api.functions = []map[string]any{{"id": 60, "slug": "fn", "name": "Fn", "input_schema": map[string]any{}, "output_schema": map[string]any{}}}
	api.versions[60] = []map[string]any{{"id": 101, "number": 1, "status": status,
		"input_schema": map[string]any{"type": "object", "required": []any{"name"}, "properties": map[string]any{
			"name": map[string]any{"type": "string"}, "rating": map[string]any{"type": "integer"}, "email": map[string]any{"type": "string"}}},
		"output_schema": map[string]any{"type": "object", "required": []any{"o"}, "properties": map[string]any{
			"o": map[string]any{"type": "string"}, "p": map[string]any{"type": "string"}}},
		"snapshot": map[string]any{"implementation": map[string]any{
			"flow": []any{"one", "two", "three"}, "sections": map[string]any{"role": "old role"}}}}}
	return api, c
}

func propNames(m map[string]any) []string {
	var out []string
	switch p := m["properties"].(type) {
	case map[string]any:
		for k := range p {
			out = append(out, k)
		}
	}
	return out
}

func TestRemove_ANewVersionWithOnlyName(t *testing.T) {
	api, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.RemoveInput("rating"))
	must(t, d.RemoveInput("email"))
	v2, err := d.SubmitAsNew("only name")
	must(t, err)
	if v2.Number != 2 || len(api.posts) != 1 {
		t.Fatalf("expected a new v2: %+v", api.posts)
	}
	sent := api.posts[0]["input_schema"].(map[string]any)
	if got := propNames(sent); !reflect.DeepEqual(got, []string{"name"}) {
		t.Fatalf("v2 should have only name: %v", got)
	}
	if !reflect.DeepEqual(sent["required"], []any{"name"}) {
		t.Fatalf("name stays required: %v", sent["required"])
	}
	if len(propNames(api.versions[60][0]["input_schema"].(map[string]any))) != 3 {
		t.Fatal("v1 must be untouched")
	}
}

func TestRemove_RequiredIsDroppedAndUnknownFieldsAreErrors(t *testing.T) {
	_, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.RemoveInput("name"))
	if _, has := d.in.jsonSchema()["required"]; has {
		t.Fatal("removing the only required field should drop `required`")
	}
	wantDefErr(t, d.RemoveInput("nope"), "not defined")
	wantDefErr(t, d.RemoveOutput("nope"), "not defined")
	must(t, d.RemoveOutput("p"))
	if len(d.out.names) != 1 {
		t.Fatalf("one output should remain: %v", d.out.names)
	}
	must(t, d.AddInput("name", map[string]any{"type": "string"})) // can be added back
}

func TestRemove_Steps(t *testing.T) {
	_, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.RemoveStep(2))
	if !reflect.DeepEqual(d.flow, []string{"one", "three"}) {
		t.Fatalf("%v", d.flow)
	}
	must(t, d.RemoveStepText("one"))
	if !reflect.DeepEqual(d.flow, []string{"three"}) {
		t.Fatalf("%v", d.flow)
	}
	wantDefErr(t, d.RemoveStep(0), "does not exist")
	wantDefErr(t, d.RemoveStep(5), "does not exist")
	wantDefErr(t, d.RemoveStepText("missing"), "no step matches")
}

func TestRemove_AnExampleUsingARemovedFieldIsCaughtAtSubmit(t *testing.T) {
	_, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	must(t, d.AddExample(ExampleOpts{Input: map[string]any{"rating": 3}, Output: map[string]any{"o": "x"}}))
	must(t, d.RemoveInput("rating"))
	_, err = d.SubmitAsNew("")
	wantDefErr(t, err, "rating")
}

func TestFresh_StartsEmptyAndNeverOverwritesADraft(t *testing.T) {
	api, c := removeSetup(t, "draft") // the case that would lose data
	var out bytes.Buffer
	changesOut = &out
	defer func() { changesOut = os.Stdout }()
	d, err := c.LoadDraft(60, NoDraftFile(), WithFresh())
	must(t, err)
	if len(d.in.names) != 0 || len(d.flow) != 0 || d.role != "" {
		t.Fatal("a fresh draft starts empty")
	}
	if len(api.posts)+len(api.puts)+len(api.submits) != 0 {
		t.Fatal("nothing is created until Submit")
	}
	must(t, d.AddInput("name", map[string]any{"required": true}))
	must(t, d.AddOutput("o", map[string]any{"required": true}))
	must(t, d.AddStep("s"))
	v2, err := d.Submit("smaller") // plain Submit, still a NEW version
	must(t, err)
	if v2.Number != 2 || len(api.puts) != 0 || len(api.posts) != 1 || api.posts[0]["parent_id"] != float64(101) {
		t.Fatalf("expected a new v2 with parent v1: puts=%v posts=%+v", api.puts, api.posts)
	}
	if len(propNames(api.versions[60][0]["input_schema"].(map[string]any))) != 3 {
		t.Fatal("v1 must keep its three inputs")
	}
	if !strings.Contains(out.String(), "- input rating") || !strings.Contains(out.String(), "- prompt") {
		t.Fatalf("the dropped parts should be printed:\n%s", out.String())
	}
	must(t, d.AddStep("t"))
	_, err = d.Submit("") // saved: later plain saves update v2 in place
	must(t, err)
	if len(api.puts) != 1 || len(api.posts) != 1 {
		t.Fatalf("a plain save should now update v2: puts=%v posts=%d", api.puts, len(api.posts))
	}
}

func TestFresh_ChangesListsDroppedAddedAndChanged(t *testing.T) {
	_, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile(), WithFresh())
	must(t, err)
	must(t, d.AddInput("name", nil)) // was required: now optional
	must(t, d.AddInput("company", nil))
	must(t, d.AddOutput("o", map[string]any{"required": true}))
	must(t, d.AddStep("two"))
	got := strings.Join(d.Changes(), "|")
	for _, want := range []string{"- input rating", "- prompt", "+ input company", "~ input name"} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %q in %s", want, got)
		}
	}
	if strings.Contains(got, "- step two") || !strings.Contains(got, "- step one") {
		t.Fatalf("steps: %s", got)
	}
	if strings.Index(got, "- input rating") > strings.Index(got, "+ input company") {
		t.Fatal("dropped parts are listed before added ones")
	}
}

func TestFresh_TheCheckAtSaveStillRuns(t *testing.T) {
	_, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile(), WithFresh())
	must(t, err)
	must(t, d.AddInput("company", nil))
	_, err = d.Submit("")
	wantDefErr(t, err, "no output fields")
	wantDefErr(t, err, "no steps")
}

func TestChanges_APlainLoadListsNothingUntilYouChangeIt(t *testing.T) {
	_, c := removeSetup(t, "approved")
	d, err := c.LoadDraft(60, NoDraftFile())
	must(t, err)
	if len(d.Changes()) != 0 {
		t.Fatalf("%v", d.Changes())
	}
	must(t, d.RemoveInput("rating"))
	must(t, d.AddInput("company", nil))
	if !reflect.DeepEqual(d.Changes(), []string{"- input rating", "+ input company"}) {
		t.Fatalf("%v", d.Changes())
	}
}

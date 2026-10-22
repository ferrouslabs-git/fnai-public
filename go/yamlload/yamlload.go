// Package yamlload reads a function back from a YAML file — a draft file the SDK kept up to date, or a
// copy made with FunctionDraft.SaveYAML — and turns it into a FunctionDraft you can carry on building.
//
// It is a separate package so the main fnai package stays free of dependencies: only programs that import
// yamlload pull in the YAML library (gopkg.in/yaml.v3). Everything else, including saving YAML, works
// without it.
package yamlload

import (
	"fmt"
	"os"

	fnai "github.com/ferrouslabs-git/fnai-public/go"
	"gopkg.in/yaml.v3"
)

// Load reads a function back from a YAML file and carries on building it. Pass the file's path, or just the
// function's name to look in the drafts folder. The file is the whole truth: it replaces what the draft
// held. If the function already exists on the server (matched by the file's meta.function) you continue it;
// otherwise it is created. The file you read is never overwritten or deleted: edits go to the draft file.
func Load(c *fnai.Client, pathOrName string, opts ...fnai.DraftOption) (*fnai.FunctionDraft, error) {
	path, err := c.DraftFile(pathOrName)
	if err != nil {
		return nil, err
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var root yaml.Node
	if err := yaml.Unmarshal(raw, &root); err != nil {
		return nil, fmt.Errorf("%s: not valid YAML: %w", path, err)
	}
	return c.LoadDocument(toTree(&root), path, opts...)
}

// toTree turns a YAML node into plain values, keeping mapping keys in the order they were written.
func toTree(n *yaml.Node) any {
	switch n.Kind {
	case yaml.DocumentNode:
		if len(n.Content) == 0 {
			return nil
		}
		return toTree(n.Content[0])
	case yaml.AliasNode:
		return toTree(n.Alias)
	case yaml.MappingNode:
		m := fnai.NewOrderedMap()
		for i := 0; i+1 < len(n.Content); i += 2 {
			m.Set(n.Content[i].Value, toTree(n.Content[i+1]))
		}
		return m
	case yaml.SequenceNode:
		list := make([]any, 0, len(n.Content))
		for _, c := range n.Content {
			list = append(list, toTree(c))
		}
		return list
	default: // scalar
		if n.Tag == "!!timestamp" {
			return n.Value
		}
		var v any
		if err := n.Decode(&v); err != nil {
			return n.Value
		}
		return v
	}
}

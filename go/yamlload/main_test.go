package yamlload_test

import (
	"os"
	"testing"
)

// TestMain keeps draft files and version copies made by tests out of the working folder.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "fnai-go-yamlload-")
	if err != nil {
		panic(err)
	}
	os.Setenv("FNAI_DRAFTS_DIR", dir)
	os.Setenv("FNAI_VERSIONS_DIR", dir)
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

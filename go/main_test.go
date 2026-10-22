package fnai

import (
	"os"
	"testing"
)

// TestMain keeps draft files made by tests out of the working folder.
func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "fnai-go-drafts-")
	if err != nil {
		panic(err)
	}
	os.Setenv("FNAI_DRAFTS_DIR", dir)
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

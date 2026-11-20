//go:build !windows

// Test build for Linux: same setup flow, no tray, so the launcher logic can be exercised off Windows.
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

const (
	pbsTriple = "x86_64-unknown-linux-gnu"
	ffName    = "ffmpeg.exe" // still fetches and unpacks the Windows ffmpeg zip, to test that path
)

var embedURL = ""

func pythonPaths(rt string) (string, string) {
	return filepath.Join(rt, "bin", "python3"), filepath.Join(rt, "bin", "python3")
}
func hidden(cmd *exec.Cmd) *exec.Cmd { return cmd }
func openURL(u string)               { fmt.Println("OPEN", u) }
func msgBox(text string)             { fmt.Println("MSGBOX", text) }
func needVC() bool                   { return false }
func makeShortcuts(string) error     { return nil }
func singleInstance() bool           { return true }
func newJob()                        {}
func attachJob(int)                  {}
func killOtherLaunchers()            {}
func refreshIcons()                  {}
func killAll() {
	if server != nil && server.Process != nil {
		server.Process.Signal(syscall.SIGTERM)
	}
}

var quitCh = make(chan bool)

func quitApp() { close(quitCh) }
func runUI() {
	go work()
	<-quitCh
	onExit()
	os.Exit(0)
}

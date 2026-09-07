package main

import (
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"
)

// sourceprobe is an operator diagnostic: it makes one bounded request through
// the same local address used by a fleet line and prints response metadata.
func main() {
	bindIP := flag.String("bind-ip", "", "local IPv4 address")
	target := flag.String("url", "", "HTTP/HTTPS URL")
	maxBytes := flag.Int64("max-bytes", 65536, "maximum response bytes to print")
	userAgent := flag.String("user-agent", "fleet-source-probe/1.0", "request User-Agent")
	flag.Parse()
	if *bindIP == "" || *target == "" || *maxBytes < 1 {
		fmt.Fprintln(os.Stderr, "usage: sourceprobe --bind-ip IPv4 --url HTTP_URL [--max-bytes N]")
		os.Exit(2)
	}
	ip := net.ParseIP(*bindIP)
	if ip == nil || ip.To4() == nil {
		fmt.Fprintln(os.Stderr, "bind-ip must be IPv4")
		os.Exit(2)
	}
	dialer := &net.Dialer{Timeout: 10 * time.Second, LocalAddr: &net.TCPAddr{IP: ip}}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DialContext = dialer.DialContext
	client := &http.Client{Transport: transport, Timeout: 20 * time.Second}
	request, err := http.NewRequest(http.MethodGet, *target, nil)
	if err != nil {
		panic(err)
	}
	request.Header.Set("User-Agent", *userAgent)
	response, err := client.Do(request)
	if err != nil {
		panic(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, *maxBytes))
	if err != nil {
		panic(err)
	}
	fmt.Printf("status=%s content_length=%d content_type=%q bytes_read=%d\n", response.Status, response.ContentLength, response.Header.Get("Content-Type"), len(body))
	fmt.Print(string(body))
}

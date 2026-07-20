package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"strings"
	"testing"
)

func TestSignAndVerifyEnvelope(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	payload := []byte(`{"schema":1}`)
	envelope, err := signEnvelope(payload, privateKey, "pumper-source-2026-01")
	if err != nil {
		t.Fatal(err)
	}
	verified, err := verifyEnvelope(envelope, publicKey, "pumper-source-2026-01", 524288)
	if err != nil || string(verified) != string(payload) {
		t.Fatalf("payload=%q err=%v", verified, err)
	}
}

func TestVerifyRejectsTampering(t *testing.T) {
	publicKey, privateKey, _ := ed25519.GenerateKey(rand.Reader)
	envelope, err := signEnvelope([]byte(`{"schema":1}`), privateKey, "expected")
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(envelope, &decoded); err != nil {
		t.Fatal(err)
	}
	for name, mutate := range map[string]func(map[string]any){
		"algorithm": func(value map[string]any) { value["algorithm"] = "ed25519" },
		"key id":    func(value map[string]any) { value["key_id"] = "other" },
		"payload":   func(value map[string]any) { value["payload"] = "e30=" },
		"signature": func(value map[string]any) { value["signature"] = "AAAA" },
	} {
		t.Run(name, func(t *testing.T) {
			copyValue := make(map[string]any, len(decoded))
			for key, value := range decoded {
				copyValue[key] = value
			}
			mutate(copyValue)
			changed, _ := json.Marshal(copyValue)
			if payload, err := verifyEnvelope(changed, publicKey, "expected", 524288); err == nil || len(payload) != 0 {
				t.Fatalf("payload=%q err=%v", payload, err)
			}
		})
	}
}

func TestVerifyRejectsMalformedDuplicateAndOversizedInput(t *testing.T) {
	publicKey, _, _ := ed25519.GenerateKey(rand.Reader)
	invalid := []string{
		`{"key_id":"a","key_id":"a","algorithm":"Ed25519","payload":"e30=","signature":"AAAA"}`,
		`{"key_id":"a","algorithm":"Ed25519","payload":"***","signature":"AAAA"}`,
		`{"key_id":"a","algorithm":"Ed25519","payload":"e30=","signature":"AAAA","extra":1}`,
		`{} {}`,
	}
	for _, input := range invalid {
		if payload, err := verifyEnvelope([]byte(input), publicKey, "a", 524288); err == nil || len(payload) != 0 {
			t.Fatalf("accepted %q", input)
		}
	}
	if payload, err := verifyEnvelope([]byte(strings.Repeat("x", 33)), publicKey, "a", 32); err == nil || len(payload) != 0 {
		t.Fatalf("accepted oversized input")
	}
}

func TestPublicKeyFromPrivateKey(t *testing.T) {
	publicKey, privateKey, _ := ed25519.GenerateKey(rand.Reader)
	derived, err := publicKeyFromPrivate(privateKey)
	if err != nil || !strings.EqualFold(string(derived), string(publicKey)) {
		t.Fatalf("derived key mismatch: err=%v", err)
	}
}

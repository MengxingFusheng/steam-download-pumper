package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
)

const defaultMaxBytes = 512 * 1024

type envelope struct {
	KeyID     string `json:"key_id"`
	Algorithm string `json:"algorithm"`
	Payload   string `json:"payload"`
	Signature string `json:"signature"`
}

func validateJSON(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	first, err := decoder.Token()
	if err != nil {
		return errors.New("invalid JSON")
	}
	if first != json.Delim('{') {
		return errors.New("JSON root must be an object")
	}
	if err := consumeObject(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return errors.New("trailing JSON data")
	}
	return nil
}

func consumeObject(decoder *json.Decoder) error {
	seen := make(map[string]struct{})
	for decoder.More() {
		token, err := decoder.Token()
		if err != nil {
			return errors.New("invalid JSON object")
		}
		key, ok := token.(string)
		if !ok {
			return errors.New("invalid JSON object key")
		}
		if _, duplicate := seen[key]; duplicate {
			return errors.New("duplicate JSON field")
		}
		seen[key] = struct{}{}
		if err := consumeValue(decoder); err != nil {
			return err
		}
	}
	closing, err := decoder.Token()
	if err != nil || closing != json.Delim('}') {
		return errors.New("invalid JSON object")
	}
	return nil
}

func consumeValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return errors.New("invalid JSON value")
	}
	delim, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		return consumeObject(decoder)
	case '[':
		for decoder.More() {
			if err := consumeValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errors.New("invalid JSON array")
		}
		return nil
	default:
		return errors.New("invalid JSON delimiter")
	}
}

func signEnvelope(payload []byte, privateKey ed25519.PrivateKey, keyID string) ([]byte, error) {
	if len(payload) == 0 || len(payload) > defaultMaxBytes {
		return nil, errors.New("payload size is invalid")
	}
	if err := validateJSON(payload); err != nil {
		return nil, err
	}
	if len(privateKey) != ed25519.PrivateKeySize || keyID == "" || len(keyID) > 128 {
		return nil, errors.New("signing parameters are invalid")
	}
	value := envelope{
		KeyID:     keyID,
		Algorithm: "Ed25519",
		Payload:   base64.StdEncoding.EncodeToString(payload),
		Signature: base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload)),
	}
	return json.Marshal(value)
}

func decodeEnvelope(input []byte) (envelope, error) {
	if err := validateJSON(input); err != nil {
		return envelope{}, err
	}
	decoder := json.NewDecoder(bytes.NewReader(input))
	decoder.DisallowUnknownFields()
	var value envelope
	if err := decoder.Decode(&value); err != nil {
		return envelope{}, errors.New("invalid envelope")
	}
	return value, nil
}

func verifyEnvelope(input []byte, publicKey ed25519.PublicKey, keyID string, maxBytes int64) ([]byte, error) {
	if maxBytes <= 0 || int64(len(input)) > maxBytes {
		return nil, errors.New("envelope exceeds size limit")
	}
	value, err := decodeEnvelope(input)
	if err != nil {
		return nil, err
	}
	if value.KeyID != keyID || value.Algorithm != "Ed25519" || keyID == "" {
		return nil, errors.New("envelope identity mismatch")
	}
	payload, err := base64.StdEncoding.Strict().DecodeString(value.Payload)
	if err != nil || len(payload) == 0 || int64(len(payload)) > maxBytes {
		return nil, errors.New("invalid envelope payload")
	}
	signature, err := base64.StdEncoding.Strict().DecodeString(value.Signature)
	if err != nil || len(signature) != ed25519.SignatureSize {
		return nil, errors.New("invalid envelope signature")
	}
	if len(publicKey) != ed25519.PublicKeySize || !ed25519.Verify(publicKey, payload, signature) {
		return nil, errors.New("signature verification failed")
	}
	if err := validateJSON(payload); err != nil {
		return nil, errors.New("signed payload is invalid")
	}
	return payload, nil
}

func readLimited(reader io.Reader, limit int64) ([]byte, error) {
	data, err := io.ReadAll(io.LimitReader(reader, limit+1))
	if err != nil {
		return nil, errors.New("input read failed")
	}
	if int64(len(data)) > limit {
		return nil, errors.New("input exceeds size limit")
	}
	return data, nil
}

func decodePrivateKey(path string) (ed25519.PrivateKey, error) {
	data, err := os.ReadFile(path)
	if err != nil || len(data) > 2048 {
		return nil, errors.New("private key is unavailable")
	}
	decoded, err := base64.StdEncoding.Strict().DecodeString(strings.TrimSpace(string(data)))
	if err != nil {
		return nil, errors.New("private key is invalid")
	}
	if len(decoded) == ed25519.SeedSize {
		return ed25519.NewKeyFromSeed(decoded), nil
	}
	if len(decoded) != ed25519.PrivateKeySize {
		return nil, errors.New("private key is invalid")
	}
	return ed25519.PrivateKey(decoded), nil
}

func publicKeyFromPrivate(privateKey ed25519.PrivateKey) (ed25519.PublicKey, error) {
	if len(privateKey) != ed25519.PrivateKeySize {
		return nil, errors.New("private key is invalid")
	}
	publicKey, ok := privateKey.Public().(ed25519.PublicKey)
	if !ok || len(publicKey) != ed25519.PublicKeySize {
		return nil, errors.New("public key derivation failed")
	}
	return append(ed25519.PublicKey(nil), publicKey...), nil
}

func runKeygen(args []string) error {
	flags := flag.NewFlagSet("keygen", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	privatePath := flags.String("private-key", "", "private key output")
	publicPath := flags.String("public-key", "", "public key output")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 || *privatePath == "" || *publicPath == "" {
		return errors.New("keygen requires private-key and public-key paths")
	}
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return errors.New("key generation failed")
	}
	privateData := []byte(base64.StdEncoding.EncodeToString(privateKey) + "\n")
	publicData := []byte(base64.StdEncoding.EncodeToString(publicKey) + "\n")
	if err := writeExclusive(*privatePath, privateData, 0o600); err != nil {
		return errors.New("private key output failed")
	}
	if err := writeExclusive(*publicPath, publicData, 0o644); err != nil {
		_ = os.Remove(*privatePath)
		return errors.New("public key output failed")
	}
	return nil
}

func writeExclusive(path string, data []byte, mode os.FileMode) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, mode)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		return err
	}
	return file.Sync()
}

func runSign(args []string) error {
	flags := flag.NewFlagSet("sign", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	privatePath := flags.String("private-key", "", "private key path")
	keyID := flags.String("key-id", "", "key identifier")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 || *privatePath == "" || *keyID == "" {
		return errors.New("sign requires private-key and key-id")
	}
	privateKey, err := decodePrivateKey(*privatePath)
	if err != nil {
		return err
	}
	payload, err := readLimited(os.Stdin, defaultMaxBytes)
	if err != nil {
		return err
	}
	output, err := signEnvelope(payload, privateKey, *keyID)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(append(output, '\n'))
	return err
}

func runVerify(args []string) error {
	flags := flag.NewFlagSet("verify", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	publicValue := flags.String("public-key-base64", "", "base64 public key")
	privatePath := flags.String("private-key", "", "private key path")
	keyID := flags.String("key-id", "", "key identifier")
	maxBytes := flags.Int64("max-bytes", defaultMaxBytes, "maximum envelope and payload bytes")
	if err := flags.Parse(args); err != nil || flags.NArg() != 0 || (*publicValue == "") == (*privatePath == "") || *keyID == "" || *maxBytes <= 0 || *maxBytes > 1024*1024 {
		return errors.New("verify parameters are invalid")
	}
	var publicKey ed25519.PublicKey
	if *privatePath != "" {
		privateKey, err := decodePrivateKey(*privatePath)
		if err != nil {
			return err
		}
		publicKey, err = publicKeyFromPrivate(privateKey)
		if err != nil {
			return err
		}
	} else {
		decoded, err := base64.StdEncoding.Strict().DecodeString(*publicValue)
		if err != nil || len(decoded) != ed25519.PublicKeySize {
			return errors.New("public key is invalid")
		}
		publicKey = ed25519.PublicKey(decoded)
	}
	input, err := readLimited(os.Stdin, *maxBytes)
	if err != nil {
		return err
	}
	payload, err := verifyEnvelope(input, publicKey, *keyID, *maxBytes)
	if err != nil {
		return err
	}
	_, err = os.Stdout.Write(payload)
	return err
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("command required: keygen, sign, or verify")
	}
	switch args[0] {
	case "keygen":
		return runKeygen(args[1:])
	case "sign":
		return runSign(args[1:])
	case "verify":
		return runVerify(args[1:])
	default:
		return errors.New("unknown command")
	}
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
}

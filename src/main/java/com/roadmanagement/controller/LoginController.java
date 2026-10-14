package com.roadmanagement.controller;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.oauth2.client.authentication.OAuth2AuthenticationToken;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;

@Controller
public class LoginController {

    @Value("${app.security.enabled:true}")
    private boolean securityEnabled;

    // Handles "/": signed-in users (or everyone, when security is disabled) go to the console
    @GetMapping("/")
    public String root(OAuth2AuthenticationToken auth) {
        if (auth == null && securityEnabled) {
            return "login"; // show login page
        }
        return "redirect:/index.html";
    }

    // Handles "/login"
    @GetMapping("/login")
    public String login() {
        return "login";
    }
}
